from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session, aliased, joinedload, load_only

from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.decision_spine_service import canonical_entity_key, name_match_score
from thoth_terminal.discord.yahoo import search_symbols
from thoth_terminal.document_readability import (
    ensure_pdf_page_image_attachments,
    maybe_hydrate_pdf_attachment_text,
    maybe_hydrate_url_document_text,
)
from thoth_terminal.document_media import canonical_media_identity, looks_like_image_url, note_image_fetch_candidates
from thoth_terminal.dataset_service import DATASET_OBJECT_TYPES, upsert_dataset_draft_row, upsert_family_dataset_materialization
from thoth_terminal.foundation_service import maybe_hydrate_transcript_document
from thoth_terminal.net import FetchError, fetch
from thoth_terminal.models import (
    AnalystEvidence,
    Attachment,
    Company,
    CompanyDocument,
    Dataset,
    Document,
    DocumentEntity,
    DocumentEvent,
    Entity,
    Issuer,
    IssuerIdentifier,
    Job,
    KnowledgeAsset,
    KnowledgeAssetVersion,
    InstitutionalEvidenceItem,
    InstitutionalObservation,
    InstrumentRequest,
    LocalAgentAction,
    LocalAgentDailySummary,
    LocalAgentDeepAnalysisTrace,
    LocalAgentDocumentCompression,
    LocalAgentDocumentRead,
    LocalAgentDocumentStageState,
    LocalAgentEntityClaim,
    LocalAgentEntityRoute,
    LocalAgentRun,
    LocalAgentTranslationOverride,
    LocalAgentUnresolvedEntity,
    LocalAgentWriteDiff,
    ResearchInstrument,
    ResearchReport,
    ResearchReportVersion,
    ResearchSubject,
    ResearchSubjectAlias,
    ResearchUniverse,
    ResearchUniverseRow,
    SecuritiesFiling,
    Site,
    Source,
    SourceActor,
    SourceActorAlias,
    StateVectorSnapshot,
    Program,
    SupplyChain,
    SupplyChainMetricBinding,
)
from thoth_terminal.local_agent_telemetry import LOCAL_AGENT_TELEMETRY_CONTRACT_VERSION, build_local_agent_telemetry
from thoth_terminal.institutional_model_vocab import (
    MODEL_VOCAB_VERSION,
    SOURCE_REGISTRY_VERSION,
    compact_metric_vocabulary,
    compact_signal_rail_taxonomy,
    normalize_metric_key,
    serialize_source_actor_for_agent,
)
try:
    from thoth_terminal.media_transcription_routing import classify_ocr_media_output
except Exception:  # pragma: no cover - optional classifier import should not break service startup
    classify_ocr_media_output = None  # type: ignore[assignment]
from thoth_terminal.object_store import abs_path as object_abs_path, guess_extension, put_bytes
from thoth_terminal.query import agent_intake_visible_filter
from thoth_terminal.research_report_service import (
    ReportSubject,
    _ensure_report_container,
    report_payload_has_detail_sections,
    resolve_report_subject,
)
from thoth_terminal.sec_filing_text import filing_review_excerpt, maybe_hydrate_sec_filing_document
from thoth_terminal.source_identity_service import find_source_actor_by_alias
from thoth_terminal.supply_chain_service import bind_supply_chain_dataset, ensure_draft_snapshot
from thoth_terminal.text import detect_language

RUN_OPEN_STATUSES = {"running"}
RUN_CLOSED_STATUSES = {"success", "error", "stopped"}
BLOCKED_DESTINATION_TYPES = {"note", "notes", "thesis", "theses", "portfolio", "ibkr", "executive_memory"}
ALLOWED_DESTINATION_TYPES = {
    "research_report",
    "knowledge_asset",
    "supply_chain",
    "dataset_or_data_series",
    "supply_chain_metric_binding",
    "company",
    "research_subject",
}
DESTINATION_TYPE_ALIASES = {
    "dataset": "dataset_or_data_series",
    "datasets": "dataset_or_data_series",
    "data_series": "dataset_or_data_series",
    "time_series": "dataset_or_data_series",
    "dataset_rows": "dataset_or_data_series",
    "dataset_materialization": "dataset_or_data_series",
    "dataset_materializations": "dataset_or_data_series",
    "metric_binding": "supply_chain_metric_binding",
    "metric_bindings": "supply_chain_metric_binding",
    "supply_chain_metric_bindings": "supply_chain_metric_binding",
}
MEDIA_TRANSCRIPTION_JOB_KIND = "media_transcription"
MEDIA_TRANSCRIPTION_EVENT_TYPE = "MEDIA_TRANSCRIBED"
TEXT_READY_RETRY_STATUS = "retry_pending"
TEXT_READY_RETRY_REASON = "document_text_ready_after_metadata_only_skip"
TRANSLATION_READY_RETRY_REASON = "translation_ready_after_metadata_only_skip"
NON_SIGNAL_MEDIA_PATTERNS = (
    "signal: non_signal",
    "signal: non-signal",
    "no semiconductor-specific data",
    "no semiconductor industry terminology",
    "no visible titles, axis labels",
    "no text, labels, charts, or data",
    "no useful source-backed",
    "no source-backed investment signal",
    "no useful investment signal",
    "purely illustrative",
    "stock photograph",
    "generic article thumbnail",
    "decorative photo",
    "profile photo",
    "avatar",
    "photo is unrelated",
    "image is unrelated",
    "brand asset",
    "company logo",
    "contains no useful investment",
    "contains no relevant investment",
    "contains no financial data",
    "contains no quantitative data",
    "does not convey any quantifiable information",
    "no quantitative data is present",
)
MODEL_OUTPUT_PARSE_ERROR_MARKERS = (
    "model returned malformed json",
    "model returned non-json content",
    "model returned empty content",
)


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _slugify(value: object, *, default: str = "agent-subject") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned[:128].strip("-") or default


def _shorten(value: object, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n...[truncated]"


def _limit(value: int | None, *, default: int, high: int) -> int:
    try:
        parsed = int(value or default)
    except Exception:
        parsed = default
    return max(1, min(parsed, high))


def _json_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _json_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _truthy_flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "signal", "useful", "relevant"}:
        return True
    if text in {"0", "false", "no", "n", "non_signal", "non-signal", "useless", "irrelevant", "discard"}:
        return False
    return None


AGENT_CONTEXT_REDACT_KEYS = {
    "durable_write_permission",
    "extraction_license",
    "language_confidence",
    "license_state",
    "ocr_quality",
    "paywall_state",
    "quote_policy",
    "reprocess_policy",
    "review_required",
    "rights_class",
    "rights_state",
    "summarization_policy",
}
AGENT_CONTEXT_CONFIDENCE_MARKERS = (
    "language",
    "layout",
    "media",
    "ocr",
    "read",
    "readability",
    "vision",
    "vlm",
)
AGENT_CONTEXT_CONFIDENCE_PATH_MARKERS = (
    "image_ocr",
    "media",
    "media_context",
    "ocr",
    "ocr_assessment",
    "structured_series",
    "vision",
    "vlm",
)


def _agent_context_should_redact_key(key: object, path: tuple[str, ...] = ()) -> bool:
    text = str(key or "").strip().lower()
    if not text:
        return False
    if text in AGENT_CONTEXT_REDACT_KEYS:
        return True
    if "paywall" in text:
        return True
    if text.startswith("rights_") or text.endswith("_rights"):
        return True
    if text.endswith("_policy") and any(marker in text for marker in ("quote", "summar", "reprocess")):
        return True
    if "confidence" in text and any(marker in text for marker in AGENT_CONTEXT_CONFIDENCE_MARKERS):
        return True
    if text == "confidence":
        joined_path = " ".join(str(part).lower() for part in path)
        return any(marker in joined_path for marker in AGENT_CONTEXT_CONFIDENCE_PATH_MARKERS)
    return False


def _strip_agent_context_noise(value: object, *, path: tuple[str, ...] = ()) -> object:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _agent_context_should_redact_key(key_text, path):
                continue
            cleaned = _strip_agent_context_noise(item, path=(*path, key_text))
            out[key_text] = cleaned
        return out
    if isinstance(value, list):
        return [_strip_agent_context_noise(item, path=path) for item in value]
    return value


def _media_signal_assessment(*, text: object, summary: object = None, detail: object = None) -> dict[str, Any]:
    detail_dict = _json_dict(detail)
    combined = _clean(f"{summary or ''} {text or ''}").lower()
    non_signal_pattern = next((pattern for pattern in NON_SIGNAL_MEDIA_PATTERNS if pattern in combined), None)
    explicit_flags = [
        flag
        for key in ("is_signal", "signal", "media_signal", "useful", "relevant")
        for flag in [_truthy_flag(detail_dict.get(key))]
        if flag is not None
    ]
    if explicit_flags:
        flag = False if False in explicit_flags else True
        if flag and non_signal_pattern:
            flag = False
        return {
            "is_signal": flag,
            "signal": "signal" if flag else "non_signal",
            "reason": str(detail_dict.get("signal_reason") or detail_dict.get("reason") or "").strip()
            or (
                f"non-signal readout overrides stored useful-media flag: {non_signal_pattern}"
                if non_signal_pattern
                else ("explicit detail flag" if not flag else "explicit useful-media flag")
            ),
        }
    if not combined:
        return {"is_signal": False, "signal": "empty", "reason": "empty media transcription"}
    if non_signal_pattern:
        return {"is_signal": False, "signal": "non_signal", "reason": non_signal_pattern}
    return {"is_signal": True, "signal": "signal", "reason": "transcribed media retained for agent context"}


def _normalize_media_structured_series(value: object) -> list[dict[str, Any]]:
    payload = value
    if isinstance(payload, dict):
        for key in ("structured_series", "series", "data_points", "points", "rows"):
            if isinstance(payload.get(key), list):
                payload = payload.get(key)
                break
    rows: list[dict[str, Any]] = []
    for raw in payload if isinstance(payload, list) else []:
        if not isinstance(raw, dict):
            continue
        row: dict[str, Any] = {}
        aliases = {
            "series": ("series", "series_name", "legend", "name"),
            "metric": ("metric", "metric_name", "measure", "label"),
            "category": ("category", "x", "x_value", "bucket"),
            "period": ("period", "date", "year", "quarter", "x_axis_value"),
            "value": ("value", "value_number", "y", "y_value"),
            "value_text": ("value_text", "raw_value", "display_value", "approx_value"),
            "unit": ("unit", "units", "value_unit"),
            "x_axis": ("x_axis", "x_axis_label"),
            "y_axis": ("y_axis", "y_axis_label"),
            "source": ("source", "source_line", "visible_source"),
            "confidence": ("confidence", "read_confidence"),
            "approximate": ("approximate", "is_approximate", "estimated"),
        }
        for dest, keys in aliases.items():
            for key in keys:
                if raw.get(key) not in (None, "", []):
                    row[dest] = raw.get(key)
                    break
        numeric = row.get("value")
        if numeric not in (None, ""):
            try:
                row["value"] = float(str(numeric).replace(",", "").replace("%", "").strip())
            except Exception:
                row["value_text"] = str(numeric)
                row.pop("value", None)
        if row.get("approximate") not in (None, ""):
            row["approximate"] = str(row.get("approximate")).strip().lower() in {"1", "true", "yes", "y", "approx", "approximate"}
        if row and any(row.get(key) not in (None, "", []) for key in ("series", "metric", "category", "period", "value", "value_text")):
            rows.append(row)
        if len(rows) >= 80:
            break
    return rows


def _media_structured_series_text(rows: object, *, max_rows: int = 12) -> str:
    parts: list[str] = []
    for row in _normalize_media_structured_series(rows)[: max(1, max_rows)]:
        label_parts = [
            str(row.get("series") or row.get("metric") or "series").strip(),
            str(row.get("period") or row.get("category") or "").strip(),
        ]
        label = " / ".join(part for part in label_parts if part)
        value = row.get("value")
        value_text = str(row.get("value_text") or "").strip()
        if value not in (None, ""):
            value_out = f"{'approx. ' if row.get('approximate') else ''}{value:g}"
        else:
            value_out = f"{'approx. ' if row.get('approximate') else ''}{value_text}".strip()
        unit = str(row.get("unit") or "").strip()
        source = str(row.get("source") or "").strip()
        suffix = f"source={source}" if source else ""
        parts.append(f"{label}: {value_out}{(' ' + unit) if unit else ''}{(' (' + suffix + ')') if suffix else ''}".strip())
    return "; ".join(part for part in parts if part)


def _image_ocr_row_signal(row: dict[str, Any]) -> dict[str, Any]:
    content_signal = _media_signal_assessment(text=row.get("text"), summary=row.get("summary"), detail={})
    explicit_flags = [
        flag
        for value in (row.get("is_signal"), row.get("signal"))
        for flag in [_truthy_flag(value)]
        if flag is not None
    ]
    if explicit_flags:
        explicit = False if False in explicit_flags else True
        if explicit and content_signal.get("is_signal") is False:
            return content_signal
        return {
            "is_signal": explicit,
            "signal": "signal" if explicit else "non_signal",
            "reason": str(row.get("signal_reason") or "").strip() or "stored media signal flag",
        }
    return _media_signal_assessment(text=row.get("text"), summary=row.get("summary"), detail=row.get("detail"))


def _document_needs_agent_translation(doc: Document) -> bool:
    if str(doc.translation_en or "").strip():
        return False
    lang = str(doc.language or "").strip().lower()
    if not lang or lang.startswith("en"):
        return False
    if doc.language_confidence is not None:
        try:
            min_conf = float(getattr(settings, "auto_translate_min_confidence", 0.55) or 0.0)
            if float(doc.language_confidence or 0.0) < min_conf:
                return False
        except Exception:
            pass
    text = str(doc.normalized_text or doc.raw_text or "").strip()
    return len(text) >= 40


def ensure_agent_document_translation(session: Session, doc: Document) -> dict[str, Any]:
    """Ensure local-agent reads prefer English text for non-English documents."""
    if not _document_needs_agent_translation(doc):
        return {
            "needed": False,
            "available": bool(str(doc.translation_en or "").strip()),
            "backend": doc.translation_backend,
            "text_chars": len(str(doc.translation_en or "")),
        }
    backend = str(getattr(settings, "default_translation_backend", "") or "googletrans")
    now = utcnow()
    extra = _json_dict(doc.extra)
    try:
        from thoth_terminal.translate import translate_document

        result = translate_document(session, document_id=int(doc.id), backend=backend, commit=False)
        text = str(result.text_en or doc.translation_en or "").strip()
        if not text:
            raise ValueError("translation backend returned empty text")
        extra["local_agent_translation_status"] = {
            "available": True,
            "backend": result.backend or doc.translation_backend or backend,
            "text_chars": len(text),
            "updated_at": _dt(now),
        }
        doc.extra = extra
        retry_pending_compressions = _mark_text_ready_compression_retries(
            session,
            document_id=int(doc.id),
            now=now,
            reason=TRANSLATION_READY_RETRY_REASON,
        )
        return {
            "needed": True,
            "available": True,
            "backend": result.backend or doc.translation_backend or backend,
            "text_chars": len(text),
            "retry_pending_compressions": retry_pending_compressions,
        }
    except Exception as exc:
        error = _shorten(f"{type(exc).__name__}: {exc}", 600)
        extra["local_agent_translation_status"] = {
            "available": False,
            "backend": backend,
            "error": error,
            "updated_at": _dt(now),
        }
        doc.extra = extra
        return {"needed": True, "available": False, "backend": backend, "error": error, "text_chars": 0}


def _translate_media_text_for_agent(doc: Document, *, text: str) -> dict[str, Any]:
    """Translate non-English media OCR/VLM text for agent context without failing writeback."""
    body = str(text or "").strip()
    if len(body) < 40:
        return {"needed": False, "available": False, "text_chars": 0}
    doc_lang = str(doc.language or "").strip().lower()
    guess = detect_language(body, min_chars=40)
    media_lang = str(guess.language or "").strip().lower()
    needs_translation = bool(media_lang and not media_lang.startswith("en"))
    if not media_lang:
        needs_translation = bool(doc_lang and not doc_lang.startswith("en"))
    if not needs_translation:
        if doc_lang and not doc_lang.startswith("en") and (not media_lang or media_lang.startswith("en")):
            return {
                "needed": True,
                "available": True,
                "language": media_lang or doc_lang,
                "backend": "identity",
                "text": body,
                "text_chars": len(body),
            }
        return {"needed": False, "available": False, "language": media_lang or doc_lang, "text_chars": 0}
    backend = str(getattr(settings, "default_translation_backend", "") or "googletrans")
    try:
        from thoth_terminal.translate import translate_text_to_english

        result = translate_text_to_english(body[:8000], title=doc.title, backend=backend)
        text_en = str(result.text_en or "").strip()
        if not text_en:
            raise ValueError("media translation backend returned empty text")
        return {
            "needed": True,
            "available": True,
            "language": media_lang or doc_lang,
            "confidence": guess.confidence,
            "backend": result.backend or backend,
            "text": text_en,
            "text_chars": len(text_en),
        }
    except Exception as exc:
        return {
            "needed": True,
            "available": False,
            "language": media_lang or doc_lang,
            "confidence": guess.confidence,
            "backend": backend,
            "error": _shorten(f"{type(exc).__name__}: {exc}", 600),
            "text_chars": 0,
        }


def _int_list(value: object) -> list[int]:
    out: list[int] = []
    for item in _json_list(value):
        if str(item).isdigit():
            parsed = int(item)
            if parsed not in out:
                out.append(parsed)
    return out


def _int(value: object) -> int | None:
    try:
        text = str(value or "").strip()
        if text.isdigit():
            return int(text)
    except Exception:
        return None
    return None


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _attachment_is_image(attachment: Attachment) -> bool:
    content_type = str(attachment.content_type or "").strip().lower()
    object_path = str(attachment.object_path or "").strip().lower()
    return (
        str(attachment.kind or "").strip().lower() == "image"
        or content_type.startswith("image/")
        or object_path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"))
    )


def _attachment_is_low_value_media_for_transcription(attachment: Attachment) -> tuple[bool, str]:
    url = str(attachment.original_url or "").strip().lower()
    path = str(attachment.object_path or "").strip().lower()
    if not url and not path:
        return True, "missing_media_locator"
    if "post_preview" in url and ("/twitter." in url or "twitter.jpg" in url):
        return True, "social_preview_image"
    if "w_48,h_48" in url or "w_32,h_32" in url:
        return True, "small_substack_chrome_image"
    dims: dict[str, int] = {}
    for key in ("w", "h"):
        match = re.search(rf"(?:^|[,/_]){key}_(\d+)(?:[,/_]|$)", url)
        if match:
            try:
                dims[key] = int(match.group(1))
            except Exception:
                pass
    if dims and max(dims.values()) <= 96:
        return True, "tiny_image"
    if dims and dims.get("w") == dims.get("h") and max(dims.values()) <= 320 and "c_fill" in url:
        return True, "substack_square_thumbnail"
    return False, ""


def _attachment_is_pdf(attachment: Attachment) -> bool:
    content_type = str(attachment.content_type or "").split(";", 1)[0].strip().lower()
    object_path = str(attachment.object_path or "").strip().lower()
    original_url = str(attachment.original_url or "").split("?", 1)[0].strip().lower()
    return (
        str(attachment.kind or "").strip().lower() == "pdf"
        or content_type == "application/pdf"
        or object_path.endswith(".pdf")
        or original_url.endswith(".pdf")
    )


def _pdf_page_attachment_source(attachment: Attachment) -> dict[str, Any]:
    original_url = str(attachment.original_url or "").strip()
    if "page=" not in original_url:
        return {}
    raw_page = original_url.rsplit("page=", 1)[-1].split("&", 1)[0].split("#", 1)[0]
    try:
        page_number = int(raw_page)
    except Exception:
        page_number = None
    doc = attachment.document if isinstance(attachment.document, Document) else None
    if doc is None:
        return {"page_number": page_number}
    for candidate in doc.attachments or []:
        if not _attachment_is_pdf(candidate):
            continue
        base = str(candidate.original_url or candidate.object_path or "").strip()
        if base and original_url.startswith(base):
            return {"source_pdf_attachment_id": int(candidate.id), "page_number": page_number}
    return {"page_number": page_number}


def _media_transcription_key(attachment: Attachment) -> str:
    stable = str(attachment.sha256 or attachment.object_path or attachment.original_url or attachment.id).strip()
    return f"media-transcription:{int(attachment.id)}:{stable}"[:255]


def _media_transcription_variant_key(row: dict[str, Any]) -> str:
    backend = str(row.get("backend") or "").strip().casefold()
    model = str(row.get("model") or "").strip().casefold()
    text = str(row.get("text") or row.get("text_en") or "")
    text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{backend}|{model}|{text_digest}"


def _media_transcription_variant(row: dict[str, Any]) -> dict[str, Any]:
    variant = {
        key: value
        for key, value in row.items()
        if key not in {"variant_key", "variants"}
    }
    variant["variant_key"] = _media_transcription_variant_key(row)
    return variant


def _upsert_image_ocr_row(
    rows: list[dict[str, Any]],
    *,
    attachment: Attachment,
    new_row: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    incoming_key = _media_transcription_variant_key(new_row)
    canonical = {**new_row, "variant_key": incoming_key}
    for index, existing in enumerate(rows):
        if not _image_ocr_row_matches(existing, attachment):
            continue
        variants: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        def add_variant(candidate: dict[str, Any]) -> None:
            variant = _media_transcription_variant(candidate)
            key = str(variant["variant_key"])
            if key == incoming_key or key in seen_keys:
                return
            seen_keys.add(key)
            variants.append(variant)

        for raw_variant in _json_list(existing.get("variants")):
            if isinstance(raw_variant, dict):
                add_variant(raw_variant)
        if _media_transcription_variant_key(existing) != incoming_key:
            add_variant(existing)
        rows[index] = {**canonical, "variants": variants}
        return rows, True

    rows.append({**canonical, "variants": []})
    return rows, False


def _serialize_media_variant_for_agent(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "variant_key",
        "backend",
        "model",
        "text",
        "text_en",
        "summary",
        "created_at",
        "media_kind",
        "ocr_quality",
        "confidence",
        "needs_vlm",
        "vlm_required",
        "vlm_available",
        "vlm_required_reasons",
        "prep_blocking",
        "ocr_signal_ready",
        "source_risk",
        "visual_signal_terms",
        "layout_labels",
        "structured_series",
        "signal",
        "is_signal",
        "signal_reason",
        "translation",
        "detail",
    )
    serialized = {
        key: row[key]
        for key in fields
        if key in row and row[key] is not None
    }
    serialized["variant_key"] = str(row.get("variant_key") or _media_transcription_variant_key(row))
    return serialized


def _image_ocr_row_matches(row: dict[str, Any], attachment: Attachment) -> bool:
    candidates = {
        "attachment_id": int(attachment.id),
        "sha256": str(attachment.sha256 or "").strip(),
        "object_path": str(attachment.object_path or "").strip(),
        "original_url": str(attachment.original_url or "").strip(),
    }
    for key, value in candidates.items():
        if not value:
            continue
        row_value = row.get(key)
        if key == "attachment_id" and str(row_value).isdigit() and int(row_value) == int(value):
            return True
        if str(row_value or "").strip() == str(value):
            return True
    row_identity = canonical_media_identity(row.get("original_url") or row.get("url"))
    attachment_identity = canonical_media_identity(attachment.original_url)
    if row_identity and row_identity == attachment_identity:
        return True
    return False


def _image_ocr_row_has_vlm_available(row: dict[str, Any]) -> bool:
    vlm_available = _truthy_flag(row.get("vlm_available"))
    if vlm_available is not None:
        return bool(vlm_available)
    detail = _json_dict(row.get("detail"))
    detail_vlm_available = _truthy_flag(detail.get("vlm_available"))
    if detail_vlm_available is not None:
        return bool(detail_vlm_available)
    backend_marker = f"{row.get('backend') or ''} {row.get('model') or ''}".lower()
    return bool(_json_dict(detail.get("vision")) or "vlm" in backend_marker or "vision" in backend_marker)


def _media_ocr_text_is_signal_ready(*, text: object, summary: object = None, ocr_quality: object = None) -> bool:
    """Return true when OCR alone is dense enough for the text-only signal pass."""
    text_value = str(text or "").strip()
    if not text_value:
        return False
    quality = str(ocr_quality or "").strip().lower()
    if quality and quality not in {"good", "high", "excellent"}:
        return False
    combined = f"{summary or ''}\n{text_value}".lower()
    if any(pattern in combined for pattern in NON_SIGNAL_MEDIA_PATTERNS):
        return False
    compact_text = _clean(text_value)
    if len(compact_text) >= 700:
        return True
    tokens = re.findall(r"[A-Za-z0-9$%][A-Za-z0-9$%./:+-]{1,}", compact_text)
    numeric_tokens = [token for token in tokens if any(ch.isdigit() for ch in token)]
    return len(tokens) >= 90 and len(numeric_tokens) >= 5


def _image_ocr_row_has_signal_ready_text(row: dict[str, Any]) -> bool:
    translation = _json_dict(row.get("translation"))
    if translation.get("needed") is True and translation.get("available") is not True:
        return False
    detail = _json_dict(row.get("detail"))
    assessment_detail = _json_dict(detail.get("ocr_assessment"))
    return _media_ocr_text_is_signal_ready(
        text=row.get("text_en") or row.get("text"),
        summary=row.get("summary"),
        ocr_quality=row.get("ocr_quality") or assessment_detail.get("ocr_quality"),
    )


OCR_ONLY_COMPLETE_MEDIA_KINDS = {
    "document_page",
    "ocr_only",
    "report_page",
    "scan_page",
    "screenshot_text",
    "spreadsheet",
    "table",
    "text_page",
}


def _image_ocr_row_allows_ocr_only_completion(row: dict[str, Any]) -> bool:
    detail = _json_dict(row.get("detail"))
    assessment = _json_dict(detail.get("ocr_assessment"))
    media_kind = str(row.get("media_kind") or detail.get("media_kind") or assessment.get("media_kind") or "").strip().lower()
    return media_kind in OCR_ONLY_COMPLETE_MEDIA_KINDS


def _image_ocr_row_is_prep_blocking(row: dict[str, Any]) -> bool:
    signal = str(row.get("signal") or "").strip().lower()
    is_signal = _truthy_flag(row.get("is_signal"))
    if is_signal is False or signal == "non_signal":
        return False
    if _image_ocr_row_has_signal_ready_text(row) and _image_ocr_row_allows_ocr_only_completion(row):
        return False
    prep_blocking = _truthy_flag(row.get("prep_blocking"))
    if prep_blocking is True:
        return True
    vlm_required = _truthy_flag(row.get("vlm_required"))
    if vlm_required is None:
        vlm_required = _truthy_flag(row.get("needs_vlm"))
    if vlm_required is None:
        detail = _json_dict(row.get("detail"))
        vlm_required = _truthy_flag(detail.get("vlm_required"))
        if vlm_required is None:
            vlm_required = _truthy_flag(detail.get("needs_vlm"))
    return bool(vlm_required is True and not _image_ocr_row_has_vlm_available(row))


def _image_ocr_row_is_agent_ready(row: dict[str, Any]) -> bool:
    signal = str(row.get("signal") or "").strip().lower()
    is_signal = _truthy_flag(row.get("is_signal"))
    if is_signal is False or signal == "non_signal":
        return True
    translation = _json_dict(row.get("translation"))
    if translation.get("needed") is True and translation.get("available") is not True:
        return False
    return not _image_ocr_row_is_prep_blocking(row)


def _document_extra_has_prep_blocking_image_ocr_for_attachment(extra_payload: object, attachment: Attachment) -> bool:
    extra = _json_dict(extra_payload)
    for row in _json_list(extra.get("image_ocr")):
        if isinstance(row, dict) and _image_ocr_row_matches(row, attachment) and _image_ocr_row_is_prep_blocking(row):
            return True
    return False


def _document_extra_has_image_ocr_for_attachment(extra_payload: object, attachment: Attachment) -> bool:
    extra = _json_dict(extra_payload)
    for row in _json_list(extra.get("image_ocr")):
        if isinstance(row, dict) and _image_ocr_row_matches(row, attachment) and _image_ocr_row_is_agent_ready(row):
            return True
    return False


def _document_has_image_ocr_for_attachment(doc: Document, attachment: Attachment) -> bool:
    return _document_extra_has_image_ocr_for_attachment(doc.extra, attachment)


def _image_url_key(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    base = raw.split("?", 1)[0].strip().lower()
    return base or raw.lower()


def _image_url_rank(value: object) -> int:
    raw = str(value or "").lower()
    if "substack-post-media" in raw and "substackcdn.com/image/fetch/" not in raw:
        return 70
    if "name=orig" in raw:
        return 60
    if "name=4096x4096" in raw or "name=large" in raw:
        return 50
    if "name=medium" in raw:
        return 40
    if "substackcdn.com/image/fetch/" in raw and "f_auto" in raw:
        return 50
    if "substackcdn.com/image/fetch/" in raw and "f_webp" in raw:
        return 45
    if "name=small" in raw:
        return 30
    if "name=240x240" in raw:
        return 20
    if "name=120x120" in raw:
        return 10
    return 25


def _attachment_media_variant_key(attachment: Attachment) -> str:
    identity = canonical_media_identity(attachment.original_url)
    if identity:
        return identity
    return f"attachment:{int(attachment.id)}"


def _preferred_media_attachments(attachments: list[Attachment]) -> list[Attachment]:
    preferred: dict[str, Attachment] = {}
    for attachment in attachments:
        key = _attachment_media_variant_key(attachment)
        current = preferred.get(key)
        candidate_rank = (_image_url_rank(attachment.original_url), int(attachment.id))
        current_rank = (
            _image_url_rank(current.original_url),
            int(current.id),
        ) if current is not None else (-1, -1)
        if current is None or candidate_rank > current_rank:
            preferred[key] = attachment
    return list(preferred.values())


def _document_image_entries(doc: Document) -> list[dict[str, Any]]:
    extra = _json_dict(doc.extra)
    entries_by_key: dict[str, dict[str, Any]] = {}
    excluded_low_value_keys = {
        canonical_media_identity(attachment.original_url) or _image_url_key(attachment.original_url)
        for attachment in _preferred_media_attachments(
            [row for row in list(doc.attachments or []) if _attachment_is_image(row)]
        )
        if _attachment_is_low_value_media_for_transcription(attachment)[0]
    }

    def add_entry(
        url: object,
        *,
        label: object = None,
        section: object = None,
        anchor: dict[str, Any] | None = None,
    ) -> None:
        raw_url = str(url or "").strip()
        key = canonical_media_identity(raw_url) or _image_url_key(raw_url)
        if not raw_url or not key or key in excluded_low_value_keys or not looks_like_image_url(raw_url):
            return
        candidate = {
            "url": raw_url,
            "label": str(label or f"Image {len(entries_by_key) + 1}").strip(),
            "section": str(section or "media").strip(),
            "key": key,
            "rank": _image_url_rank(raw_url),
            "anchor": {
                key: value
                for key in ("heading", "caption", "alt", "preceding_text", "following_text", "index", "dom_index")
                for value in [(anchor or {}).get(key)]
                if value not in (None, "", [], {})
            },
        }
        existing = entries_by_key.get(key)
        if existing is None or int(candidate["rank"]) > int(existing.get("rank") or 0):
            entries_by_key[key] = candidate

    manifest = extra.get("media_manifest")
    if isinstance(manifest, list):
        for row in manifest:
            if not isinstance(row, dict):
                continue
            if str(row.get("kind") or "image").strip().lower() != "image":
                continue
            add_entry(row.get("url"), label=row.get("label"), section=row.get("section"), anchor=row)

    for key in ("root_media", "media", "image_urls"):
        raw = extra.get(key)
        for url in raw if isinstance(raw, list) else ([raw] if raw else []):
            add_entry(url, section="post")

    return list(entries_by_key.values())


def _document_image_ocr_url_keys(doc: Document) -> set[str]:
    extra = _json_dict(doc.extra)
    keys: set[str] = set()
    for row in _json_list(extra.get("image_ocr")):
        if not isinstance(row, dict):
            continue
        for key_name in ("original_url", "url"):
            key = _image_url_key(row.get(key_name))
            if key:
                keys.add(key)
    return keys


def _ensure_document_media_url_attachments(session: Session, doc: Document, *, max_images: int, max_bytes: int) -> int:
    entries = _document_image_entries(doc)
    if not entries:
        return 0
    existing_rows = [row for row in list(doc.attachments or []) if _attachment_is_image(row)]
    existing_url_keys = {_image_url_key(row.original_url) for row in existing_rows if row.original_url}
    existing_sha = {str(row.sha256 or "") for row in existing_rows if row.sha256}
    ocr_url_keys = _document_image_ocr_url_keys(doc)
    remaining = max(0, min(max_images, len(entries)) - len(existing_rows))
    if remaining <= 0:
        return 0

    added = 0
    errors: list[str] = []
    for entry in entries:
        if added >= remaining:
            break
        url = str(entry.get("url") or "").strip()
        key = _image_url_key(url)
        if not url or key in existing_url_keys or key in ocr_url_keys:
            continue
        stored = False
        candidate_errors: list[str] = []
        for fetch_url in note_image_fetch_candidates(url):
            fetch_key = _image_url_key(fetch_url)
            if fetch_key in existing_url_keys or fetch_key in ocr_url_keys:
                stored = True
                break
            try:
                result = fetch(fetch_url, timeout_s=10)
                data = bytes(result.content or b"")
                if not data:
                    candidate_errors.append(f"{fetch_url}: empty image response")
                    continue
                if max_bytes > 0 and len(data) > max_bytes:
                    candidate_errors.append(f"{fetch_url}: image too large")
                    continue
                content_type = str(result.content_type or "").split(";", 1)[0].strip().lower() or None
                if content_type and not content_type.startswith("image/"):
                    candidate_errors.append(f"{fetch_url}: non-image content-type {content_type}")
                    continue
                ext = guess_extension(content_type=content_type, url=fetch_url) or ".jpg"
                object_path, sha = put_bytes(data, ext=ext)
                if sha in existing_sha:
                    existing_url_keys.add(fetch_key)
                    stored = True
                    break
                session.add(
                    Attachment(
                        document_id=int(doc.id),
                        kind="image",
                        original_url=fetch_url,
                        content_type=content_type,
                        object_path=object_path,
                        sha256=sha,
                        created_at=utcnow(),
                    )
                )
                existing_sha.add(sha)
                existing_url_keys.add(fetch_key)
                added += 1
                stored = True
                break
            except FetchError as exc:
                candidate_errors.append(f"{fetch_url}: {exc}")
            except Exception as exc:
                candidate_errors.append(f"{fetch_url}: {type(exc).__name__}: {exc}")
        if not stored and candidate_errors:
            errors.extend(candidate_errors[:3])

    if added or errors:
        extra = _json_dict(doc.extra)
        extra["image_media_backfill_status"] = {
            "attempted_at": _dt(utcnow()),
            "added": added,
            "errors": errors[:5],
        }
        doc.extra = extra
    return added


def _agent_can_access_document(session: Session, document_id: int) -> bool:
    return (
        session.query(Document.id)
        .filter(Document.id == int(document_id))
        .filter(agent_intake_visible_filter(session))
        .first()
        is not None
    )


def _agent_visible_document_ids(session: Session):
    return session.query(Document.id).filter(agent_intake_visible_filter(session))


def _agent_visible_document_or_null(session: Session, column: Any):
    return or_(column.is_(None), column.in_(_agent_visible_document_ids(session)))


def _diff(before: str | None, after: str | None, *, label: str = "agent-write") -> str:
    before_lines = str(before or "").splitlines()
    after_lines = str(after or "").splitlines()
    return "\n".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{label}:before",
            tofile=f"{label}:after",
            lineterm="",
        )
    )


def _destination_type(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    raw = DESTINATION_TYPE_ALIASES.get(raw, raw)
    if raw in BLOCKED_DESTINATION_TYPES:
        raise ValueError(f"Destination type `{raw}` is blocked for the local analyst agent.")
    if raw not in ALLOWED_DESTINATION_TYPES:
        raise ValueError(f"Unsupported local-agent destination type: {raw}")
    return raw


def serialize_run(row: LocalAgentRun, *, include_stats: bool = True) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_type": row.run_type,
        "status": row.status,
        "model_profile": row.model_profile,
        "model_name": row.model_name,
        "prompt_version": row.prompt_version,
        "worker_id": row.worker_id,
        "summary": row.summary,
        "stats": dict(row.stats_json or {}) if include_stats else {},
        "error": row.error,
        "started_at": _dt(row.started_at),
        "completed_at": _dt(row.completed_at),
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def serialize_action(row: LocalAgentAction, *, include_payloads: bool = True) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "action_kind": row.action_kind,
        "status": row.status,
        "tool_name": row.tool_name,
        "endpoint": row.endpoint,
        "document_id": int(row.document_id) if row.document_id is not None else None,
        "destination_type": row.destination_type,
        "destination_id": int(row.destination_id) if row.destination_id is not None else None,
        "summary": row.summary,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "request": dict(row.request_json or {}) if include_payloads else {},
        "response": dict(row.response_json or {}) if include_payloads else {},
        "error": row.error,
        "created_at": _dt(row.created_at),
    }


def serialize_agent_document(row: LocalAgentDailySummary, *, include_payload: bool = True) -> dict[str, Any]:
    kind = str(row.summary_kind or "").strip() or "agent_dispatch"
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "summary_date": row.summary_date.isoformat() if row.summary_date else None,
        "summary_kind": kind,
        "display_kind": _agent_document_display_kind(kind=kind, title=row.title),
        "title": row.title,
        "display_title": _agent_document_display_title(kind=kind, title=row.title, summary_date=row.summary_date),
        "content_md": row.content_md,
        "source_document_ids": list(row.source_document_ids_json or []),
        "compression_ids": list(row.compression_ids_json or []),
        "payload": dict(row.payload or {}) if include_payload else {},
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def list_stale_running_runs(
    session: Session,
    *,
    stale_after_minutes: int = 60,
    limit: int = 20,
) -> list[dict[str, Any]]:
    minutes = max(1, min(int(stale_after_minutes or 60), 24 * 60))
    cutoff = utcnow() - timedelta(minutes=minutes)
    now = utcnow()
    rows = (
        session.query(LocalAgentRun)
        .filter(LocalAgentRun.status.in_(RUN_OPEN_STATUSES))
        .order_by(LocalAgentRun.started_at.asc(), LocalAgentRun.id.asc())
        .limit(500)
        .all()
    )
    stale: list[dict[str, Any]] = []
    for row in rows:
        last_action_at = (
            session.query(func.max(LocalAgentAction.created_at))
            .filter(LocalAgentAction.run_id == int(row.id))
            .scalar()
        )
        last_seen_at = max(
            [value for value in (row.updated_at, last_action_at, row.started_at) if value is not None],
            default=row.started_at,
        )
        if last_seen_at is None or last_seen_at > cutoff:
            continue
        payload = serialize_run(row)
        payload.update(
            {
                "last_action_at": _dt(last_action_at),
                "last_seen_at": _dt(last_seen_at),
                "age_minutes": round((now - row.started_at).total_seconds() / 60, 1) if row.started_at else None,
                "idle_minutes": round((now - last_seen_at).total_seconds() / 60, 1),
                "stale_after_minutes": minutes,
            }
        )
        stale.append(payload)
    stale.sort(key=lambda item: str(item.get("last_seen_at") or ""), reverse=True)
    return stale[: _limit(limit, default=20, high=1000)]


def local_agent_run_health(
    session: Session,
    *,
    stale_after_minutes: int = 60,
    limit: int = 20,
) -> dict[str, Any]:
    stale_rows = list_stale_running_runs(session, stale_after_minutes=stale_after_minutes, limit=limit)
    stale_count = len(list_stale_running_runs(session, stale_after_minutes=stale_after_minutes, limit=1000))
    running_count = session.query(LocalAgentRun).filter(LocalAgentRun.status.in_(RUN_OPEN_STATUSES)).count()
    return {
        "running_runs": int(running_count or 0),
        "stale_after_minutes": max(1, min(int(stale_after_minutes or 60), 24 * 60)),
        "stale_running_runs": stale_count,
        "stale_runs": stale_rows,
    }


def serialize_compression(row: LocalAgentDocumentCompression) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "document_id": int(row.document_id),
        "model": row.model,
        "prompt_version": row.prompt_version,
        "summary_short": row.summary_short,
        "summary_detail": row.summary_detail,
        "atomic_claims": list(row.atomic_claims_json or []),
        "entity_refs": list(row.entity_refs_json or []),
        "supply_chain_refs": list(row.supply_chain_refs_json or []),
        "technology_refs": list(row.technology_refs_json or []),
        "unresolved_entities": list(row.unresolved_entities_json or []),
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "information_gain": row.information_gain,
        "document_disposition": row.document_disposition,
        "skip_reason": row.skip_reason,
        "status": row.status,
        "error": row.error,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def serialize_deep_analysis_trace(row: LocalAgentDeepAnalysisTrace, *, include_payload: bool = True) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "compression_id": int(row.compression_id),
        "signal_extraction_id": int(row.compression_id),
        "document_id": int(row.document_id),
        "model": row.model,
        "prompt_version": row.prompt_version,
        "analysis_mode": row.analysis_mode,
        "status": row.status,
        "signal_level": row.signal_level,
        "signal_score": float(row.signal_score) if row.signal_score is not None else None,
        "summary_md": row.summary_md,
        "key_findings": list(row.key_findings_json or []),
        "knowledge_connections": list(row.knowledge_connections_json or []),
        "contradictions": list(row.contradictions_json or []),
        "open_questions": list(row.open_questions_json or []),
        "followups": list(row.followups_json or []),
        "payload": dict(row.payload or {}) if include_payload else {},
        "error": row.error,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def serialize_claim(row: LocalAgentEntityClaim) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "compression_id": int(row.compression_id) if row.compression_id is not None else None,
        "document_id": int(row.document_id) if row.document_id is not None else None,
        "entity_type": row.entity_type,
        "entity_id": int(row.entity_id) if row.entity_id is not None else None,
        "entity_name": row.entity_name,
        "destination_type": row.destination_type,
        "destination_id": int(row.destination_id) if row.destination_id is not None else None,
        "claim_kind": row.claim_kind,
        "claim_text": row.claim_text,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "routing_reason": row.routing_reason,
        "payload": dict(row.payload or {}),
        "created_at": _dt(row.created_at),
    }


def _claim_dedupe_key(row: LocalAgentEntityClaim) -> tuple[object, ...]:
    scope = ("document", int(row.document_id)) if row.document_id is not None else ("compression", int(row.compression_id) if row.compression_id is not None else None)
    return (
        scope,
        _clean(row.entity_type).casefold(),
        int(row.entity_id) if row.entity_id is not None else None,
        _clean(row.entity_name).casefold(),
        _clean(row.destination_type).casefold() or None,
        int(row.destination_id) if row.destination_id is not None else None,
        _clean(row.claim_kind).casefold(),
        _clean(row.claim_text).casefold(),
    )


def _find_existing_claim(session: Session, row: LocalAgentEntityClaim) -> LocalAgentEntityClaim | None:
    if row.document_id is None and row.compression_id is None:
        return None
    query = session.query(LocalAgentEntityClaim)
    if row.document_id is not None:
        query = query.filter(LocalAgentEntityClaim.document_id == int(row.document_id))
    else:
        query = query.filter(LocalAgentEntityClaim.compression_id == int(row.compression_id))
    query = query.filter(
        LocalAgentEntityClaim.entity_id == row.entity_id,
        LocalAgentEntityClaim.destination_type == row.destination_type,
        LocalAgentEntityClaim.destination_id == row.destination_id,
        LocalAgentEntityClaim.claim_kind == row.claim_kind,
    )
    target_key = _claim_dedupe_key(row)
    for existing in query.order_by(LocalAgentEntityClaim.id.asc()).limit(250):
        if _claim_dedupe_key(existing) == target_key:
            setattr(existing, "_thoth_deduped", True)
            return existing
    return None


def serialize_route(row: LocalAgentEntityRoute) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "compression_id": int(row.compression_id) if row.compression_id is not None else None,
        "document_id": int(row.document_id) if row.document_id is not None else None,
        "lane": row.lane,
        "entity_name": row.entity_name,
        "entity_type": row.entity_type,
        "entity_role": row.entity_role,
        "summary_relevance": bool(row.summary_relevance),
        "summary_confidence": float(row.summary_confidence) if row.summary_confidence is not None else None,
        "durable_relevance": bool(row.durable_relevance),
        "durable_confidence": float(row.durable_confidence) if row.durable_confidence is not None else None,
        "durable_action": row.durable_action,
        "materiality": row.materiality,
        "novelty": row.novelty,
        "temporal_posture": row.temporal_posture,
        "evidence_tier": row.evidence_tier,
        "resolution_status": row.resolution_status,
        "new_information": row.new_information,
        "rationale": row.rationale,
        "claim_refs": list(row.claim_refs_json or []),
        "source_claim_indexes": list(row.source_claim_indexes_json or []),
        "destination_type": row.destination_type,
        "destination_id": int(row.destination_id) if row.destination_id is not None else None,
        "payload": dict(row.payload or {}),
        "created_at": _dt(row.created_at),
    }


def _serialize_claim_for_daily_summary(row: LocalAgentEntityClaim) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "compression_id": int(row.compression_id) if row.compression_id is not None else None,
        "document_id": int(row.document_id) if row.document_id is not None else None,
        "entity_type": row.entity_type,
        "entity_id": int(row.entity_id) if row.entity_id is not None else None,
        "entity_name": row.entity_name,
        "destination_type": row.destination_type,
        "destination_id": int(row.destination_id) if row.destination_id is not None else None,
        "claim_kind": row.claim_kind,
        "claim_text": row.claim_text,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "routing_reason": row.routing_reason,
        "payload": {},
        "created_at": _dt(row.created_at),
    }


def _serialize_route_for_daily_summary(row: LocalAgentEntityRoute) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "compression_id": int(row.compression_id) if row.compression_id is not None else None,
        "document_id": int(row.document_id) if row.document_id is not None else None,
        "lane": row.lane,
        "entity_name": row.entity_name,
        "entity_type": row.entity_type,
        "entity_role": row.entity_role,
        "summary_relevance": bool(row.summary_relevance),
        "summary_confidence": float(row.summary_confidence) if row.summary_confidence is not None else None,
        "durable_relevance": bool(row.durable_relevance),
        "durable_confidence": float(row.durable_confidence) if row.durable_confidence is not None else None,
        "durable_action": row.durable_action,
        "materiality": row.materiality,
        "novelty": row.novelty,
        "temporal_posture": row.temporal_posture,
        "evidence_tier": row.evidence_tier,
        "resolution_status": row.resolution_status,
        "new_information": row.new_information,
        "rationale": row.rationale,
        "claim_refs": list(row.claim_refs_json or []),
        "source_claim_indexes": list(row.source_claim_indexes_json or []),
        "destination_type": row.destination_type,
        "destination_id": int(row.destination_id) if row.destination_id is not None else None,
        "payload": {},
        "created_at": _dt(row.created_at),
    }


def serialize_write_diff(row: LocalAgentWriteDiff) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "action_id": int(row.action_id) if row.action_id is not None else None,
        "destination_type": row.destination_type,
        "destination_id": int(row.destination_id) if row.destination_id is not None else None,
        "source_document_ids": list(row.source_document_ids_json or []),
        "before_version_id": int(row.before_version_id) if row.before_version_id is not None else None,
        "after_version_id": int(row.after_version_id) if row.after_version_id is not None else None,
        "diff_text": row.diff_text,
        "write_summary": row.write_summary,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "payload": dict(row.payload or {}),
        "created_at": _dt(row.created_at),
    }


def record_action(
    session: Session,
    *,
    run_id: int | None = None,
    action_kind: str,
    status: str = "success",
    tool_name: str | None = None,
    endpoint: str | None = None,
    document_id: int | None = None,
    destination_type: str | None = None,
    destination_id: int | None = None,
    summary: str | None = None,
    confidence: float | None = None,
    request_json: dict[str, Any] | None = None,
    response_json: dict[str, Any] | None = None,
    error: str | None = None,
) -> LocalAgentAction:
    created_at = utcnow()
    normalized_run_id = int(run_id) if run_id is not None and int(run_id) > 0 else None
    row = LocalAgentAction(
        run_id=normalized_run_id,
        action_kind=str(action_kind).strip()[:64],
        status=str(status or "success").strip().lower()[:16],
        tool_name=str(tool_name).strip()[:128] if tool_name else None,
        endpoint=str(endpoint).strip()[:255] if endpoint else None,
        document_id=int(document_id) if document_id is not None else None,
        destination_type=str(destination_type).strip()[:64] if destination_type else None,
        destination_id=int(destination_id) if destination_id is not None else None,
        summary=str(summary).strip() if summary else None,
        confidence=float(confidence) if confidence is not None else None,
        request_json=dict(request_json or {}),
        response_json=dict(response_json or {}),
        error=str(error).strip() if error else None,
        created_at=created_at,
    )
    session.add(row)
    if normalized_run_id is not None:
        run = session.query(LocalAgentRun).filter_by(id=normalized_run_id).first()
        if run is not None and run.status in RUN_OPEN_STATUSES:
            run.updated_at = created_at
    session.flush()
    return row


def start_run(
    session: Session,
    *,
    run_type: str,
    model_profile: str | None = None,
    model_name: str | None = None,
    prompt_version: str | None = None,
    worker_id: str | None = None,
    stats: dict[str, Any] | None = None,
) -> LocalAgentRun:
    now = utcnow()
    row = LocalAgentRun(
        run_type=str(run_type or "feed_triage").strip()[:64],
        status="running",
        model_profile=str(model_profile or getattr(settings, "agent_default_model", "") or "").strip()[:128] or None,
        model_name=str(model_name or "").strip()[:255] or None,
        prompt_version=str(prompt_version or getattr(settings, "agent_prompt_version", "local-analyst-v3-staged-signal-context-search") or "").strip()[:64] or None,
        worker_id=str(worker_id or "").strip()[:128] or None,
        summary=None,
        stats_json={
            **dict(stats or {}),
            "telemetry": {
                "contract_version": LOCAL_AGENT_TELEMETRY_CONTRACT_VERSION,
                "run": {"duration_ms": None, "status": "running"},
                "actions": {"total": 0, "success": 0, "errors": 0, "running": 0, "error_rate": None},
                "tools": {"calls": 0, "errors": 0, "error_rate": None, "by_tool": {}, "truncated": False},
                "model": {"available": False, "calls": 0},
                "stages": {"available": False, "duration_ms_by_stage": {}},
            },
        },
        error=None,
        started_at=now,
        completed_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    record_action(session, run_id=int(row.id), action_kind="run_start", tool_name="agent_start_run", summary=row.run_type)
    return row


def finish_run(
    session: Session,
    *,
    run_id: int,
    status: str,
    summary: str | None = None,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> LocalAgentRun:
    row = session.query(LocalAgentRun).filter_by(id=int(run_id)).first()
    if row is None:
        raise ValueError("Local agent run not found.")
    status_v = str(status or "success").strip().lower()
    if status_v not in RUN_OPEN_STATUSES | RUN_CLOSED_STATUSES:
        raise ValueError("Invalid local agent run status.")
    now = utcnow()
    row.status = status_v
    row.summary = str(summary).strip() if summary else row.summary
    final_stats = dict(stats or row.stats_json or {})
    row.error = str(error).strip() if error else None
    row.completed_at = None if status_v in RUN_OPEN_STATUSES else now
    row.updated_at = now
    record_action(
        session,
        run_id=int(row.id),
        action_kind="run_finish",
        status="error" if status_v == "error" else "success",
        tool_name="agent_finish_run",
        summary=row.summary or status_v,
        error=row.error,
    )
    final_stats["telemetry"] = build_local_agent_telemetry(session, run=row, stats=final_stats)
    row.stats_json = final_stats
    return row


def stop_stale_running_runs(
    session: Session,
    *,
    stale_after_minutes: int = 60,
    limit: int = 20,
    dry_run: bool = True,
) -> dict[str, Any]:
    minutes = max(1, min(int(stale_after_minutes or 60), 24 * 60))
    rows = list_stale_running_runs(session, stale_after_minutes=minutes, limit=limit)
    out: list[dict[str, Any]] = []
    for stale_row in rows:
        run_id = int(stale_row["id"])
        summary = f"Stopped stale local-agent run after {stale_row.get('idle_minutes')} idle minutes."
        if dry_run:
            payload = dict(stale_row)
            payload["would_stop"] = True
            out.append(payload)
            continue
        stopped = finish_run(
            session,
            run_id=run_id,
            status="stopped",
            summary=summary,
            stats=stale_row.get("stats") if isinstance(stale_row.get("stats"), dict) else None,
        )
        payload = serialize_run(stopped)
        payload.update(
            {
                "stopped": True,
                "last_action_at": stale_row.get("last_action_at"),
                "last_seen_at": stale_row.get("last_seen_at"),
                "age_minutes": stale_row.get("age_minutes"),
                "idle_minutes": stale_row.get("idle_minutes"),
                "stale_after_minutes": minutes,
            }
        )
        out.append(payload)
    return {
        "dry_run": bool(dry_run),
        "stale_after_minutes": minutes,
        "count": len(out),
        "runs": out,
    }


def cleanup_local_agent_traces(session: Session, *, retention_hours: int | None = None) -> dict[str, int]:
    hours = max(1, int(retention_hours or getattr(settings, "agent_trace_retention_hours", 72) or 72))
    cutoff = utcnow() - timedelta(hours=hours)
    counts: dict[str, int] = {}
    counts["write_diffs"] = (
        session.query(LocalAgentWriteDiff)
        .filter(LocalAgentWriteDiff.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    counts["entity_routes"] = (
        session.query(LocalAgentEntityRoute)
        .filter(LocalAgentEntityRoute.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    counts["document_reads"] = (
        session.query(LocalAgentDocumentRead)
        .filter(LocalAgentDocumentRead.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    counts["actions"] = (
        session.query(LocalAgentAction)
        .filter(LocalAgentAction.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    return counts


def _document_media_context(doc: Document, *, max_chars: int = 3000) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    extra = _json_dict(doc.extra)
    status = _json_dict(extra.get("image_ocr_status"))
    ocr_rows: list[dict[str, Any]] = []
    media_signal_updated = False
    for raw_row in _json_list(extra.get("image_ocr")):
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        signal = _image_ocr_row_signal(row)
        if "is_signal" not in row or "signal" not in row or "signal_reason" not in row:
            row["is_signal"] = bool(signal.get("is_signal"))
            row["signal"] = signal.get("signal")
            row["signal_reason"] = signal.get("reason")
            media_signal_updated = True
        ocr_rows.append(row)
    if media_signal_updated:
        extra["image_ocr"] = ocr_rows
        doc.extra = extra
    ocr_by_key: dict[str, dict[str, Any]] = {}
    for row in ocr_rows:
        for key_name in ("original_url", "object_path", "url"):
            key = str(row.get(key_name) or "").strip()
            if key:
                ocr_by_key.setdefault(key, row)
                image_key = _image_url_key(key)
                if image_key:
                    ocr_by_key.setdefault(image_key, row)
                identity = canonical_media_identity(key)
                if identity:
                    ocr_by_key.setdefault(identity, row)

    entries: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def add_entry(
        url: object,
        *,
        label: object = None,
        section: object = None,
        anchor: dict[str, Any] | None = None,
    ) -> None:
        raw_url = str(url or "").strip()
        image_key = _image_url_key(raw_url)
        identity_key = canonical_media_identity(raw_url) or image_key
        if not raw_url or not image_key or identity_key in seen_keys:
            return
        seen_keys.add(identity_key)
        ocr = ocr_by_key.get(raw_url) or ocr_by_key.get(identity_key) or ocr_by_key.get(image_key) or {}
        signal = _image_ocr_row_signal(ocr) if ocr else {}
        translation = _json_dict(ocr.get("translation")) if ocr else {}
        translation_pending = bool(translation.get("needed") is True and translation.get("available") is not True)
        text_en = str(ocr.get("text_en") or "").strip()
        source_text = str(ocr.get("text") or "").strip()
        ocr_text = text_en or ("" if translation_pending else source_text)
        ocr_text_source = "text_en" if text_en else "translation_pending" if translation_pending else "text"
        is_signal = signal.get("is_signal") if signal else None
        detail = _json_dict(ocr.get("detail")) if ocr else {}
        needs_vlm = _truthy_flag(ocr.get("needs_vlm"))
        vlm_required = _truthy_flag(ocr.get("vlm_required"))
        if vlm_required is None:
            vlm_required = needs_vlm
        vlm_available = _truthy_flag(ocr.get("vlm_available"))
        if vlm_available is None:
            backend_marker = f"{ocr.get('backend') or ''} {ocr.get('model') or ''}".lower()
            vlm_available = bool(detail.get("vision") or "vlm" in backend_marker or "vision" in backend_marker)
        ocr_signal_ready = bool(ocr and _image_ocr_row_has_signal_ready_text(ocr))
        prep_blocking = bool(_image_ocr_row_is_prep_blocking(ocr)) if ocr else False
        if not prep_blocking and vlm_required is True and not vlm_available:
            vlm_required = False
        if translation_pending and is_signal is not False:
            is_signal = None
        if prep_blocking and is_signal is not False:
            is_signal = None
        entries.append(
            {
                "url": raw_url,
                "label": str(label or f"Image {len(entries) + 1}").strip(),
                "section": str(section or "media").strip(),
                "backend": str(ocr.get("backend") or "").strip() or None,
                "model": str(ocr.get("model") or "").strip() or None,
                "ocr_summary": str(ocr.get("summary") or "").strip(),
                "ocr_text": ocr_text,
                "ocr_text_source": ocr_text_source,
                "variants": [
                    _serialize_media_variant_for_agent(variant)
                    for variant in _json_list(ocr.get("variants"))
                    if isinstance(variant, dict)
                ],
                "translation": translation,
                "translation_pending": translation_pending,
                "media_kind": str(ocr.get("media_kind") or "").strip() or None,
                "ocr_quality": str(ocr.get("ocr_quality") or "").strip() or None,
                "needs_vlm": needs_vlm,
                "vlm_required": vlm_required,
                "vlm_available": vlm_available,
                "vlm_pending": bool(prep_blocking),
                "prep_blocking": bool(prep_blocking),
                "ocr_signal_ready": ocr_signal_ready,
                "vlm_required_reasons": [str(value) for value in _json_list(ocr.get("vlm_required_reasons") or detail.get("vlm_required_reasons"))[:8]],
                "source_risk": str(ocr.get("source_risk") or "").strip() or None,
                "visual_signal_terms": [str(value) for value in _json_list(ocr.get("visual_signal_terms"))[:12]],
                "layout_labels": [str(value) for value in _json_list(ocr.get("layout_labels"))[:12]],
                "structured_series": _normalize_media_structured_series(ocr.get("structured_series") or detail.get("structured_series")),
                "media_signal": str(signal.get("signal") or ("pending" if not ocr else "signal")).strip(),
                "is_signal": is_signal,
                "signal_reason": str(signal.get("reason") or "").strip() or None,
                "source_anchor": dict(anchor or {}),
            }
        )

    for entry in _document_image_entries(doc):
        add_entry(
            entry.get("url"),
            label=entry.get("label"),
            section=entry.get("section"),
            anchor=entry.get("anchor") if isinstance(entry.get("anchor"), dict) else None,
        )

    for attachment in doc.attachments or []:
        if not _attachment_is_image(attachment):
            continue
        if _attachment_is_low_value_media_for_transcription(attachment)[0]:
            continue
        key = str(attachment.original_url or attachment.object_path or "").strip()
        section = "pdf_page" if "page=" in str(attachment.original_url or "") else "attachment"
        add_entry(key, label="Stored image", section=section)

    for row in ocr_rows:
        key = str(row.get("original_url") or row.get("object_path") or row.get("url") or "").strip()
        image_key = canonical_media_identity(key) or _image_url_key(key)
        if key and image_key not in seen_keys:
            add_entry(key, label="OCR image", section="ocr")

    if not entries and not status:
        return "", [], {}

    lines: list[str] = []
    if entries:
        lines.append("Media context:")
        for entry in entries[:8]:
            label = entry.get("label") or "Image"
            section = entry.get("section") or "media"
            ocr_summary = str(entry.get("ocr_summary") or "").strip()
            ocr_text = str(entry.get("ocr_text") or "").strip()
            if ocr_summary and ocr_text and ocr_summary not in ocr_text:
                ocr_text = f"{ocr_summary} {ocr_text}"
            elif not ocr_text:
                ocr_text = ocr_summary
            media_kind = entry.get("media_kind") or "media"
            if entry.get("is_signal") is False:
                reason = entry.get("signal_reason") or "non-signal media"
                lines.append(f"- {label} ({section}, {media_kind}): non-signal media omitted from compression context: {reason}")
            elif entry.get("vlm_pending"):
                lines.append(
                    f"- {label} ({section}, {media_kind}): pending VLM visual description; OCR text held from compression context until chart/diagram readout succeeds."
                )
            elif entry.get("translation_pending"):
                lines.append(
                    f"- {label} ({section}, {media_kind}): pending media translation; untranslated OCR/VLM text omitted from compression context."
                )
            elif ocr_text:
                if entry.get("needs_vlm") is True and entry.get("vlm_available") is True:
                    needs = "VLM readout retained"
                elif entry.get("needs_vlm") is True and entry.get("ocr_signal_ready") is True:
                    needs = "dense OCR retained; VLM not blocking"
                elif entry.get("needs_vlm") is True:
                    needs = "needs VLM"
                else:
                    needs = "OCR retained"
                terms = ", ".join(str(value) for value in _json_list(entry.get("visual_signal_terms"))[:6])
                suffix = f"; visual terms: {terms}" if terms else ""
                series_text = _media_structured_series_text(entry.get("structured_series"))
                series_suffix = f" Structured visual data: {_shorten(series_text, 600)}" if series_text else ""
                lines.append(f"- {label} ({section}, {media_kind}, {needs}{suffix}): {_shorten(ocr_text, 900)}{series_suffix}")
            else:
                lines.append(
                    f"- {label} ({section}): pending OCR; original image URL: {entry.get('url')}"
                )
        if len(entries) > 8:
            lines.append(f"- {len(entries) - 8} additional image(s) omitted from agent payload.")
    if status and not status.get("available"):
        lines.append("OCR status: unavailable; image text may be missing from this document.")

    text = "\n".join(lines).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[: max(200, max_chars - 18)].rstrip() + "\n...[truncated]"
    return text, entries, status


def backfill_legacy_media_classifications(
    session: Session,
    *,
    limit: int = 100,
    scan_limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Classify stored image_ocr rows that predate media_kind/is_signal metadata."""
    max_updates = _limit(limit, default=100, high=1000)
    max_scan = _limit(scan_limit, default=max(1000, max_updates * 20), high=10000)
    scan_offset = max(0, int(offset or 0))
    docs = (
        session.query(Document)
        .options(joinedload(Document.source))
        .filter(Document.extra.is_not(None))
        .order_by(Document.ingested_at.desc(), Document.id.desc())
        .offset(scan_offset)
        .limit(max_scan)
        .all()
    )
    candidate_docs = len(docs)
    scanned_docs = 0
    updated_docs = 0
    updated_rows = 0
    now = utcnow()
    for doc in docs:
        extra = _json_dict(doc.extra)
        rows = [dict(row) for row in _json_list(extra.get("image_ocr")) if isinstance(row, dict)]
        if not rows:
            continue
        scanned_docs += 1
        if updated_docs >= max_updates:
            continue
        changed = False
        source = doc.source if isinstance(doc.source, Source) else None
        for row in rows:
            stored_is_signal = _truthy_flag(row.get("is_signal"))
            stored_signal = _truthy_flag(row.get("signal"))
            conflicting_signal = stored_is_signal is not None and stored_signal is not None and stored_is_signal != stored_signal
            stored_media_kind = _clean(row.get("media_kind")).lower()
            misleading_non_signal_kind = (
                (stored_is_signal is False or stored_signal is False)
                and stored_media_kind not in {"", "non_signal_image"}
            )
            missing = any(
                key not in row or row.get(key) in (None, "", [])
                for key in ("is_signal", "signal", "signal_reason", "media_kind", "ocr_quality", "needs_vlm")
            ) or conflicting_signal or misleading_non_signal_kind
            if not missing:
                continue
            detail = _json_dict(row.get("detail"))
            inferred: dict[str, Any] = {}
            if classify_ocr_media_output is not None:
                try:
                    assessment = classify_ocr_media_output(
                        f"{row.get('summary') or ''}\n{row.get('text') or ''}",
                        route=detail.get("route") if isinstance(detail.get("route"), dict) else None,
                        layout_details=detail.get("layout_details") or detail.get("json_result"),
                        source_context={
                            "source_name": source.name if source is not None else None,
                            "document_url": doc.url or doc.canonical_url,
                            "document_title": doc.title,
                            "original_url": row.get("original_url"),
                        },
                    )
                    inferred = assessment.as_dict() if hasattr(assessment, "as_dict") else dict(assessment or {})
                except Exception:
                    inferred = {}
            signal = _image_ocr_row_signal({**row, **inferred})
            row["is_signal"] = bool(signal.get("is_signal"))
            row["signal"] = signal.get("signal")
            row["signal_reason"] = signal.get("reason")
            if signal.get("is_signal") is False:
                row["media_kind"] = "non_signal_image"
                row["needs_vlm"] = False
            for key in ("media_kind", "ocr_quality", "needs_vlm", "source_risk", "visual_signal_terms", "layout_labels"):
                if key not in row or row.get(key) in (None, "", []) or (conflicting_signal and key in {"media_kind", "needs_vlm", "source_risk", "visual_signal_terms", "layout_labels"}):
                    value = inferred.get(key)
                    if value not in (None, "", []):
                        row[key] = value
            row.setdefault("classification_backfilled_at", _dt(now))
            changed = True
            updated_rows += 1
        if not changed:
            continue
        extra["image_ocr"] = rows
        extra["image_ocr_status"] = {
            **_json_dict(extra.get("image_ocr_status")),
            "classification_backfilled_at": _dt(now),
        }
        doc.extra = extra
        session.add(
            DocumentEvent(
                document_id=int(doc.id),
                event_type="MEDIA_CLASSIFICATION_BACKFILLED",
                event_at=now,
                meta={"rows": len(rows), "updated_rows": updated_rows},
            )
        )
        updated_docs += 1
    session.flush()
    return {
        "candidate_documents": candidate_docs,
        "scanned_documents": scanned_docs,
        "updated_documents": updated_docs,
        "updated_rows": updated_rows,
        "limit": max_updates,
        "scan_limit": max_scan,
        "offset": scan_offset,
        "next_offset": scan_offset + candidate_docs if candidate_docs >= max_scan else None,
        "more_candidates": candidate_docs >= max_scan,
    }


def backfill_media_ocr_translations(
    session: Session,
    *,
    limit: int = 100,
    scan_limit: int | None = None,
    offset: int = 0,
    document_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Translate useful non-English image_ocr rows that predate media text_en writeback."""
    max_updates = _limit(limit, default=100, high=1000)
    max_scan = _limit(scan_limit, default=max(1000, max_updates * 20), high=10000)
    scan_offset = max(0, int(offset or 0))
    clean_document_ids = [int(value) for value in (document_ids or []) if str(value).isdigit()]
    query = (
        session.query(Document)
        .options(joinedload(Document.source))
        .filter(Document.extra.is_not(None))
        .order_by(Document.ingested_at.desc(), Document.id.desc())
    )
    if clean_document_ids:
        query = query.filter(Document.id.in_(clean_document_ids))
    else:
        query = query.offset(scan_offset).limit(max_scan)
    docs = query.all()
    candidate_docs = len(docs)
    scanned_docs = 0
    updated_docs = 0
    updated_rows = 0
    translated_rows = 0
    failed_rows = 0
    skipped_non_signal_rows = 0
    skipped_existing_rows = 0
    skipped_not_needed_rows = 0
    skipped_too_short_rows = 0
    translated_chars = 0
    now = utcnow()
    for doc in docs:
        extra = _json_dict(doc.extra)
        rows = [dict(row) for row in _json_list(extra.get("image_ocr")) if isinstance(row, dict)]
        if not rows:
            continue
        scanned_docs += 1
        if updated_docs >= max_updates:
            continue
        changed = False
        doc_updated_rows = 0
        doc_translated_rows = 0
        doc_failed_rows = 0
        for row in rows:
            signal = _image_ocr_row_signal(row)
            if signal.get("is_signal") is False:
                skipped_non_signal_rows += 1
                continue
            translation = _json_dict(row.get("translation"))
            if str(row.get("text_en") or "").strip() and translation.get("available") is True:
                skipped_existing_rows += 1
                continue
            text = str(row.get("text") or row.get("summary") or "").strip()
            if len(text) < 40:
                skipped_too_short_rows += 1
                continue
            media_translation = _translate_media_text_for_agent(doc, text=text)
            if not media_translation.get("needed"):
                if translation.get("needed") is True:
                    row["translation"] = {key: value for key, value in media_translation.items() if key != "text"}
                    row["media_translation_backfilled_at"] = _dt(now)
                    changed = True
                    doc_updated_rows += 1
                    updated_rows += 1
                else:
                    skipped_not_needed_rows += 1
                continue
            row["translation"] = {key: value for key, value in media_translation.items() if key != "text"}
            row["media_translation_backfilled_at"] = _dt(now)
            if media_translation.get("available") and str(media_translation.get("text") or "").strip():
                text_en = str(media_translation.get("text") or "").strip()
                row["text_en"] = text_en
                translated_rows += 1
                doc_translated_rows += 1
                translated_chars += len(text_en)
            else:
                failed_rows += 1
                doc_failed_rows += 1
            changed = True
            doc_updated_rows += 1
            updated_rows += 1
        if not changed:
            continue
        extra["image_ocr"] = rows
        extra["image_ocr_status"] = {
            **_json_dict(extra.get("image_ocr_status")),
            "media_translation_backfilled_at": _dt(now),
        }
        doc.extra = extra
        session.add(
            DocumentEvent(
                document_id=int(doc.id),
                event_type="MEDIA_TRANSLATION_BACKFILLED",
                event_at=now,
                meta={"updated_rows": doc_updated_rows, "translated_rows": doc_translated_rows, "failed_rows": doc_failed_rows},
            )
        )
        updated_docs += 1
    session.flush()
    return {
        "candidate_documents": candidate_docs,
        "scanned_documents": scanned_docs,
        "updated_documents": updated_docs,
        "updated_rows": updated_rows,
        "translated_rows": translated_rows,
        "failed_rows": failed_rows,
        "translated_chars": translated_chars,
        "skipped_non_signal_rows": skipped_non_signal_rows,
        "skipped_existing_rows": skipped_existing_rows,
        "skipped_not_needed_rows": skipped_not_needed_rows,
        "skipped_too_short_rows": skipped_too_short_rows,
        "document_ids": clean_document_ids,
        "limit": max_updates,
        "scan_limit": max_scan,
        "offset": scan_offset,
        "next_offset": None if clean_document_ids else scan_offset + candidate_docs if candidate_docs >= max_scan else None,
        "more_candidates": False if clean_document_ids else candidate_docs >= max_scan,
    }


def document_payload(
    doc: Document,
    *,
    max_text_chars: int = 18000,
    stored_text_override: str | None = None,
) -> dict[str, Any]:
    if stored_text_override is not None:
        stored_text_source = "queue_projection"
        stored_text = str(stored_text_override)
    elif str(doc.translation_en or "").strip():
        stored_text_source = "translation_en"
        stored_text = str(doc.translation_en or "")
    elif str(doc.normalized_text or "").strip():
        stored_text_source = "normalized_text"
        stored_text = str(doc.normalized_text or "")
    else:
        stored_text_source = "raw_text"
        stored_text = str(doc.raw_text or "")
    original_stored_chars = len(stored_text)
    text = stored_text
    review_excerpt = (
        None
        if stored_text_override is not None
        else filing_review_excerpt(doc, max_chars=min(8_000, max(1_200, int(max_text_chars or 0) // 2)))
    )
    if review_excerpt:
        text = f"SEC filing review:\n{review_excerpt}\n\nFull filing text:\n{stored_text}"
    source_text = text
    source_text_truncated = False
    if max_text_chars and len(source_text) > max_text_chars:
        source_text = source_text[: max(1000, int(max_text_chars))]
        source_text_truncated = True
    media_limit = min(3_000, max(600, int(max_text_chars or 9000) // 3))
    media_context, media_entries, ocr_status = _document_media_context(doc, max_chars=media_limit)
    context_ocr_status = _strip_agent_context_noise(ocr_status, path=("image_ocr_status",)) if ocr_status else {}
    payload_truncated = False
    if media_context:
        if max_text_chars:
            max_total = max(1000, int(max_text_chars))
            text_budget = max(500, max_total - len(media_context) - 20)
            if len(text) > text_budget:
                text = text[:text_budget].rstrip() + "\n...[truncated]"
                payload_truncated = True
        text = f"{text.rstrip()}\n\n{media_context}".strip()
    if max_text_chars and len(text) > max_text_chars:
        text = text[: max(1000, int(max_text_chars))]
        payload_truncated = True
    source = doc.source if isinstance(doc.source, Source) else None
    doc_extra = _json_dict(doc.extra)
    agent_extra = {
        key: doc_extra.get(key)
        for key in (
            "connector",
            "handle",
            "tweet_id",
            "article_url",
            "quoted_url",
            "links",
            "thread_context",
        )
        if doc_extra.get(key) is not None
    }
    translation_available = False if stored_text_override is not None else bool(str(doc.translation_en or "").strip())
    translation_backend = None if stored_text_override is not None else doc.translation_backend
    translation_chars = 0 if stored_text_override is not None else len(str(doc.translation_en or ""))
    translation_status = _json_dict(doc_extra.get("local_agent_translation_status"))
    lang = str(doc.language or "").strip().lower()
    translation_needed = bool(
        translation_available
        or (stored_text_override is None and _document_needs_agent_translation(doc))
        or (translation_status.get("available") is False and bool(str(translation_status.get("error") or "").strip()))
    )
    translation_attempted = bool(translation_status)
    inline_translation_required = bool(
        translation_needed
        and not translation_available
        and stored_text_override is None
        and translation_attempted
    )
    translation_pending = bool(translation_needed and not translation_available and not inline_translation_required)
    pending_media_count = sum(1 for entry in media_entries if entry.get("is_signal") is None)
    payload = {
        "id": int(doc.id),
        "kind": doc.kind,
        "source_id": int(doc.source_id),
        "source_name": source.name if source else None,
        "source_category": source.category if source else None,
        "title": doc.title,
        "author": doc.author,
        "url": doc.url or doc.canonical_url,
        "published_at": _dt(doc.published_at),
        "ingested_at": _dt(doc.ingested_at),
        "triage_state": doc.triage_state,
        "importance": int(doc.importance or 0),
        "language": doc.language,
        "extra": agent_extra,
        "prep_status": {
            "stage_order": [
                "text_hydration",
                "pdf_text_extraction",
                "pdf_page_image_render",
                "translation_to_english",
                "image_ocr_vlm",
                "agent_payload_pack",
                "signal_extraction",
                "durable_reasoning",
            ],
            "text_source": stored_text_source,
            "stored_text_chars": original_stored_chars,
            "payload_text_chars": len(text),
            "translation": {
                "needed": translation_needed,
                "available": translation_available,
                "backend": translation_backend,
                "text_chars": translation_chars,
                "source_language": lang or None,
                "output_language": "en" if translation_needed else None,
                "status": translation_status or None,
                "attempted": translation_attempted,
                "inline_signal_extraction_fallback": inline_translation_required,
                "mode": (
                    "stored_translation"
                    if translation_available
                    else "agent_inline_translation_required"
                    if inline_translation_required
                    else "pending_translation"
                    if translation_needed
                    else "not_required"
                ),
            },
            "media": {
                "entries": len(media_entries),
                "signal_count": sum(1 for entry in media_entries if entry.get("is_signal") is True),
                "non_signal_count": sum(1 for entry in media_entries if entry.get("is_signal") is False),
                "pending_count": pending_media_count,
                "ocr_status": context_ocr_status,
                "pdf_page_image_render": doc_extra.get("pdf_page_image_render"),
            },
            "prep_complete": not translation_pending and pending_media_count == 0,
        },
        "context_management": {
            "max_text_chars": int(max_text_chars or 0),
            "stored_text_source": stored_text_source,
            "stored_text_chars": original_stored_chars,
            "payload_text_chars": len(text),
            "truncated": payload_truncated,
            "media_context_appended": bool(media_context),
            "structured_media_separate": bool(media_entries),
            "source_text_chars": len(source_text),
            "source_text_truncated": source_text_truncated,
            "output_language": "en",
            "inline_translation_required": inline_translation_required,
        },
        "source_text": source_text,
        "text": text,
    }
    if media_entries:
        payload["media"] = _strip_agent_context_noise(media_entries, path=("media",))
    if ocr_status:
        payload["image_ocr_status"] = context_ocr_status
    return payload


def document_analysis_readiness(session: Session, *, document_id: int) -> dict[str, Any]:
    doc = (
        session.query(Document)
        .options(joinedload(Document.source), joinedload(Document.attachments))
        .filter_by(id=int(document_id))
        .first()
    )
    if doc is None:
        raise ValueError("Document not found.")
    payload = document_payload(doc, max_text_chars=1_000)
    prep_status = _json_dict(payload.get("prep_status"))
    return {
        "document_id": int(doc.id),
        "ready": bool(prep_status.get("prep_complete")),
        "prep_status": prep_status,
    }


def _source_actor_context_candidates(document: Document | None, query: str | None) -> list[str]:
    candidates: list[str] = []

    def add(value: object) -> None:
        text = _clean(value)
        if text and text.lower() not in {item.lower() for item in candidates}:
            candidates.append(text)

    if query:
        add(query)
    if document is not None:
        source = document.source if isinstance(document.source, Source) else None
        extra = _json_dict(document.extra)
        add(document.author)
        add(source.name if source is not None else None)
        add(extra.get("handle"))
        add(extra.get("source_name"))
        add(extra.get("underlying_source_name"))
        add(extra.get("underlying_firm"))
    return candidates[:12]


def _source_actor_matches(session: Session, *, document: Document | None, query: str | None, limit: int) -> list[dict[str, Any]]:
    matches: list[SourceActor] = []

    def add(actor: SourceActor | None) -> None:
        if actor is None:
            return
        if not any(int(row.id) == int(actor.id) for row in matches):
            matches.append(actor)

    for candidate in _source_actor_context_candidates(document, query):
        handle = candidate.lstrip("@")
        add(find_source_actor_by_alias(session, alias=handle, alias_kind="x_handle", platform="x"))
        add(find_source_actor_by_alias(session, alias=candidate, alias_kind="name"))
        add(find_source_actor_by_alias(session, alias=candidate, alias_kind="firm_name"))
        if len(matches) >= limit:
            break
    if len(matches) < limit and query:
        pattern = f"%{_clean(query)}%"
        for row in (
            session.query(SourceActor)
            .outerjoin(SourceActorAlias, SourceActorAlias.actor_id == SourceActor.id)
            .filter(or_(SourceActor.display_name.ilike(pattern), SourceActorAlias.alias.ilike(pattern)))
            .order_by(SourceActor.updated_at.desc(), SourceActor.id.desc())
            .limit(limit)
            .all()
        ):
            add(row)
    return [serialize_source_actor_for_agent(row) for row in matches[:limit]]


def _source_actor_registry_sample(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = (
        session.query(SourceActor)
        .filter(SourceActor.status != "archived")
        .filter(or_(SourceActor.side.in_(("sell_side", "specialist_research")), SourceActor.actor_type.in_(("research_org", "data_vendor"))))
        .order_by(SourceActor.display_name.asc())
        .limit(max(1, limit * 4))
        .all()
    )
    registry_rows = [
        row
        for row in rows
        if SOURCE_REGISTRY_VERSION == str((_json_dict(row.payload)).get("source_registry") or "")
        or row.side in {"sell_side", "specialist_research"}
    ]
    return [serialize_source_actor_for_agent(row, alias_limit=5) for row in registry_rows[:limit]]


def _research_subject_matches(session: Session, *, query: str | None, document: Document | None, limit: int) -> list[dict[str, Any]]:
    candidates = [_clean(query)]
    if document is not None:
        candidates.extend([_clean(document.title), _clean(document.author)])
    terms = [item for item in candidates if item]
    if not terms:
        return []
    rows: list[ResearchSubject] = []

    def add(row: ResearchSubject) -> None:
        if not any(int(existing.id) == int(row.id) for existing in rows):
            rows.append(row)

    for term in terms[:4]:
        if len(term) > 80:
            continue
        pattern = f"%{term}%"
        for row in (
            session.query(ResearchSubject)
            .outerjoin(ResearchSubjectAlias, ResearchSubjectAlias.subject_id == ResearchSubject.id)
            .filter(
                or_(
                    ResearchSubject.display_name.ilike(pattern),
                    ResearchSubject.legal_name.ilike(pattern),
                    ResearchSubject.slug.ilike(pattern),
                    ResearchSubject.subject_key.ilike(pattern),
                    ResearchSubjectAlias.alias.ilike(pattern),
                )
            )
            .order_by(ResearchSubject.updated_at.desc(), ResearchSubject.id.desc())
            .limit(limit)
            .all()
        ):
            add(row)
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        aliases = [
            {"alias": alias.alias, "alias_type": alias.alias_type}
            for alias in session.query(ResearchSubjectAlias).filter_by(subject_id=int(row.id)).order_by(ResearchSubjectAlias.alias.asc()).limit(8).all()
        ]
        out.append(
            {
                "id": int(row.id),
                "subject_key": row.subject_key,
                "slug": row.slug,
                "kind": row.kind,
                "display_name": row.display_name,
                "company_id": row.company_id,
                "issuer_id": row.issuer_id,
                "aliases": aliases,
            }
        )
    return out


def _state_vectors_for_context(session: Session, *, subject_id: int | None, limit: int) -> list[dict[str, Any]]:
    q = session.query(StateVectorSnapshot).order_by(StateVectorSnapshot.updated_at.desc(), StateVectorSnapshot.id.desc())
    if subject_id is not None:
        q = q.filter(StateVectorSnapshot.subject_id == int(subject_id))
    rows = q.limit(max(1, limit)).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row.id),
                "snapshot_key": row.snapshot_key,
                "scope_type": row.scope_type,
                "scope_key": row.scope_key,
                "subject_id": row.subject_id,
                "installed_capacity": row.installed_capacity,
                "effective_capacity": row.effective_capacity,
                "utilization": row.utilization,
                "yield_rate": row.yield_rate,
                "lead_time_days": row.lead_time_days,
                "inventory_days": row.inventory_days,
                "allocation_status": row.allocation_status,
                "qualification_status": row.qualification_status,
                "demand_signal": row.demand_signal,
                "schedule_status": row.schedule_status,
                "logistics_friction": row.logistics_friction,
                "confidence": row.confidence,
                "freshness_at": _dt(row.freshness_at),
                "latest_metrics": (_json_dict(row.payload)).get("latest_metrics") or [],
            }
        )
    return out


DURABLE_AGENT_DESTINATIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "analyst_evidence",
        "purpose": "Source-owned consensus estimates, ratings, model deltas, and professional claims.",
        "required_fields": ["estimate_owner", "metric_key", "value_or_status", "period", "source_document_id"],
    },
    {
        "key": "institutional_observation",
        "purpose": "Normalized model-delta observations/state-vector inputs derived from analyst_evidence.",
        "required_fields": ["metric_key", "value_number_or_text", "subject", "period_or_as_of", "source"],
    },
    {
        "key": "dataset_or_data_series",
        "purpose": "Repeatable structured time series, benchmark tables, capacity tables, and operating datasets.",
        "required_fields": ["dataset_family", "metric_key", "value", "unit", "period", "source"],
    },
    {
        "key": "ai_serving_economics",
        "purpose": "AI serving economics rails. Prefer datasets `artificial-analysis-model-economics`, `ai-model-cost-quality-frontier`, `inferencex-upstream-benchmarks`, `ai-provider-api-pricing`, `ai-serving-scenario-assumptions`, and `ai-serving-margin-scenarios` for model quality, API price, throughput, utilization/cache, power, and capex assumptions.",
        "required_fields": ["dataset_slug", "metric_key", "value", "unit", "scenario_or_model", "source_or_assumption"],
    },
    {
        "key": "supply_chain_metric_binding",
        "purpose": "Capacity, lead-time, logistics, bottleneck, and node/edge metrics bound to supply-chain graphs.",
        "required_fields": ["supply_chain_or_node", "metric_kind", "value_or_status", "period", "source"],
    },
    {
        "key": "research_request",
        "purpose": "Precise missing-rail/schema requests when a fact cannot be represented without losing owner, segment, value, or period.",
        "required_fields": ["missing_fact_class", "required_fields", "source_evidence", "why_existing_rails_are_lossy"],
    },
    {
        "key": "company_or_report_section",
        "purpose": "Narrative company-page/report updates only after modelable source-owned facts are also captured as evidence/observations.",
        "required_fields": ["subject", "section", "source_document_id", "source_summary"],
    },
)


def _instrument_request_context_matches(
    session: Session,
    *,
    query: str | None,
    document: Document | None,
    subject_row: ResearchSubject | None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    term_source = " ".join(
        str(value or "")
        for value in (
            query,
            document.title if document is not None else None,
            document.source.name if document is not None and isinstance(document.source, Source) else None,
            subject_row.display_name if subject_row is not None else None,
            subject_row.subject_key if subject_row is not None else None,
        )
    )
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-]{2,}", term_source)][:20]
    candidates = (
        session.query(InstrumentRequest)
        .filter(InstrumentRequest.status.in_(["draft", "review", "live"]))
        .order_by(InstrumentRequest.priority.desc(), InstrumentRequest.updated_at.desc(), InstrumentRequest.id.desc())
        .limit(250)
        .all()
    )

    def score(row: InstrumentRequest) -> int:
        text = " ".join(
            [
                row.title or "",
                row.intent or "",
                row.summary or "",
                row.provenance_summary or "",
                " ".join(str(value) for value in (row.target_entities or [])),
                " ".join(str(value) for value in (row.target_themes or [])),
                " ".join(str(value) for value in (row.desired_outputs or [])),
                json.dumps(row.draft_payload or {}, ensure_ascii=False, default=str)[:1600],
            ]
        ).lower()
        match_score = sum(1 for term in terms if term and term in text)
        if row.intent in {"missing_research_rail", "schema_gap", "company_resolution"}:
            match_score += 2
        if row.source_kind and any(token in str(row.source_kind).lower() for token in ("missing", "rail", "schema")):
            match_score += 2
        return match_score

    out: list[dict[str, Any]] = []
    for row in sorted(candidates, key=score, reverse=True):
        row_score = score(row)
        if terms and row_score <= 0:
            continue
        out.append(
            {
                "id": int(row.id),
                "request_key": row.request_key,
                "title": row.title,
                "intent": row.intent,
                "status": row.status,
                "priority": row.priority,
                "source_kind": row.source_kind,
                "target_entities": list(row.target_entities or [])[:8],
                "target_themes": list(row.target_themes or [])[:8],
                "desired_outputs": list(row.desired_outputs or [])[:8],
                "summary": _shorten(row.summary, 900),
                "provenance_summary": _shorten(row.provenance_summary, 700),
                "match_score": row_score,
            }
        )
        if len(out) >= limit:
            break
    return out


def institutional_model_context(
    session: Session,
    *,
    document_id: int | None = None,
    query: str | None = None,
    subject_id: int | None = None,
    max_source_actors: int = 12,
    max_subjects: int = 8,
    max_state_vectors: int = 8,
) -> dict[str, Any]:
    document = session.get(Document, int(document_id)) if document_id is not None else None
    subject_row = session.get(ResearchSubject, int(subject_id)) if subject_id is not None else None
    if subject_row is None and document is not None:
        extra = _json_dict(document.extra)
        company_slug = str(extra.get("company_slug") or "").strip()
        if company_slug:
            company = session.query(Company).filter_by(slug=company_slug).first()
            if company is not None:
                subject_row = session.query(ResearchSubject).filter_by(company_id=int(company.id)).first()
    resolved_subject_id = int(subject_row.id) if subject_row is not None else None
    state_vectors = _state_vectors_for_context(session, subject_id=resolved_subject_id, limit=max_state_vectors)
    recent_observation_count = 0
    if resolved_subject_id is not None:
        recent_observation_count = (
            session.query(InstitutionalObservation)
            .filter(InstitutionalObservation.subject_id == resolved_subject_id)
            .count()
        )
    return {
        "version": MODEL_VOCAB_VERSION,
        "source_registry_version": SOURCE_REGISTRY_VERSION,
        "skill_card": {
            "purpose": "Resolve institutional source actors and modelable variables before writing analyst evidence or model deltas.",
            "source_identity_rule": "Use matched source_actor ids when present. If a person/account, firm, watermark, or source line is not matched, preserve raw text and emit an unresolved alias gap rather than creating a duplicate canonical actor.",
            "subject_identity_rule": "Use matched research_subject ids/aliases only when provided. For ambiguous materials, products, tickers, or abbreviations, preserve raw_subject_text and mark requires_resolution=true.",
            "metric_rule": "Use canonical_metric_key from metric_vocabulary. Percent_bounded is only for 0-100 measures such as market share, yield, utilization, mix, penetration, and attach rate.",
            "rail_rule": "Use signal_rail_taxonomy to choose the durable destination. If a fact class is absent or required fields cannot be preserved, emit a precise research_request instead of a lossy prose write.",
            "ai_serving_economics_rule": "For GPU inference economics, use existing AI datasets before prose: InferenceX for throughput/power, Artificial Analysis for intelligence/cost/frontier, provider pricing for token revenue, serving assumptions for utilization/cache/capex, and serving margin scenarios for derived economics. Mark utilization, cache hit rate, price realization, and capex as assumptions unless explicitly sourced.",
            "period_rule": "Normalize explicit CY/FY years, quarters, and halves; do not infer a period from background prose.",
        },
        "metric_vocabulary": compact_metric_vocabulary(include_aliases=True),
        "signal_rail_taxonomy": compact_signal_rail_taxonomy(),
        "durable_rail_catalog": {
            "supported_destinations": list(DURABLE_AGENT_DESTINATIONS),
            "missing_rail_request_matches": _instrument_request_context_matches(
                session,
                query=query,
                document=document,
                subject_row=subject_row,
                limit=8,
            ),
        },
        "period_grammar": {
            "year": ["CY2026", "FY2027", "2026"],
            "quarter": ["CY2026Q3", "Q3 FY2026"],
            "half": ["1H CY2026", "2H FY2027"],
            "point_in_time": "Use as_of_date for spot capacity, lead time, allocation, or qualification status.",
        },
        "source_actor_matches": _source_actor_matches(session, document=document, query=query, limit=max_source_actors),
        "source_registry_sample": _source_actor_registry_sample(session, limit=max_source_actors),
        "subject_matches": _research_subject_matches(session, query=query, document=document, limit=max_subjects),
        "state_vectors": state_vectors,
        "recent_observation_count": recent_observation_count,
        "report_input_contract": {
            "bottleneck_reports": ["evidence_items", "institutional_observations", "state_vectors", "coverage_gaps", "model_scenarios"],
            "company_deep_dives": ["analyst_evidence", "model_deltas", "state_vectors", "datasets", "open_gaps"],
        },
    }


def enqueue_pending_media_transcription_jobs(
    session: Session,
    *,
    limit: int = 100,
    render_readable_pdf_pages: bool = False,
    document_ids: list[int] | None = None,
) -> dict[str, Any]:
    max_rows = _limit(limit, default=100, high=1000)
    target_document_ids = _int_list(document_ids)[:50]
    backfilled_documents = 0
    backfilled_attachments = 0
    pdf_page_rendered_documents = 0
    pdf_page_rendered_attachments = 0
    backfill_scan_cap = min(100, max(20, max_rows * 5))
    backfill_attachment_cap = max_rows
    document_recency_for_backfill = func.coalesce(Document.published_at, Document.ingested_at)
    media_docs_query = (
        session.query(Document)
        # This polling path only needs document identity, compact media metadata,
        # and attachment rows. Loading raw_text/normalized_text/translation_en for
        # every candidate can turn a small queue poll into a multi-gigabyte result.
        .options(
            load_only(Document.id, Document.extra),
            joinedload(Document.attachments),
        )
        .filter(agent_intake_visible_filter(session))
    )
    if target_document_ids:
        media_docs_query = media_docs_query.filter(Document.id.in_(target_document_ids))
    media_docs = (
        media_docs_query
        .order_by(document_recency_for_backfill.desc(), Document.id.desc())
        .limit(max(len(target_document_ids), backfill_scan_cap) if target_document_ids else backfill_scan_cap)
        .all()
    )
    max_images = max(1, min(int(getattr(settings, "ocr_max_images_per_doc", 6) or 6), 20))
    max_bytes = max(100_000, int(getattr(settings, "ocr_max_image_bytes", 5_000_000) or 5_000_000))
    for doc in media_docs:
        if backfilled_attachments >= backfill_attachment_cap:
            break
        if _document_image_entries(doc):
            added = _ensure_document_media_url_attachments(
                session,
                doc,
                max_images=max_images,
                max_bytes=max_bytes,
            )
            if added:
                backfilled_documents += 1
                backfilled_attachments += added
        if backfilled_attachments >= backfill_attachment_cap:
            break
        # PDF rendering is an expensive, allocation-heavy mutation. Never perform
        # it merely because Raider polled the queue; callers must opt in.
        if render_readable_pdf_pages:
            render_pages = max(1, min(max_images, backfill_attachment_cap - backfilled_attachments))
            page_render = ensure_pdf_page_image_attachments(
                session,
                doc,
                render_readable_text=True,
                max_pages=render_pages,
            )
        else:
            page_render = {"attempted": False, "reason": "render_not_requested"}
        stored_pages = int(page_render.get("stored_attachments") or 0) if page_render.get("attempted") else 0
        if stored_pages > 0:
            pdf_page_rendered_documents += 1
            pdf_page_rendered_attachments += stored_pages
            backfilled_attachments += stored_pages
    if backfilled_attachments:
        session.flush()

    object_path_lower = func.lower(func.coalesce(Attachment.object_path, ""))
    content_type_lower = func.lower(func.coalesce(Attachment.content_type, ""))
    document_recency = func.coalesce(Document.published_at, Document.ingested_at, Attachment.created_at)
    base_query = (
        session.query(Attachment)
        .join(Document, Document.id == Attachment.document_id)
        # Attachment scans inspect only document.extra. Eager-loading the full
        # document repeats large text values across thousands of joined rows.
        .options(joinedload(Attachment.document).load_only(Document.id, Document.extra))
        .filter(agent_intake_visible_filter(session))
        .filter(
            or_(
                func.lower(func.coalesce(Attachment.kind, "")) == "image",
                content_type_lower.like("image/%"),
                object_path_lower.like("%.png"),
                object_path_lower.like("%.jpg"),
                object_path_lower.like("%.jpeg"),
                object_path_lower.like("%.webp"),
                object_path_lower.like("%.gif"),
                object_path_lower.like("%.bmp"),
                object_path_lower.like("%.tif"),
                object_path_lower.like("%.tiff"),
            )
        )
    )
    if target_document_ids:
        base_query = base_query.filter(Attachment.document_id.in_(target_document_ids))
    base_query = base_query.order_by(document_recency.desc(), Attachment.id.desc())
    preferred_target_attachment_ids: set[int] | None = None
    if target_document_ids:
        target_attachments = base_query.limit(2_000).all()
        preferred_target_attachment_ids = {
            int(row.id) for row in _preferred_media_attachments(target_attachments)
        }
    enqueued = 0
    reopened_prep_blocking = 0
    settled_duplicate_variants = 0
    skipped_existing = 0
    skipped_duplicate_variants = 0
    skipped_non_image = 0
    skipped_low_value = 0
    scanned = 0
    scan_cap = min(2_000, max(500, max_rows * 20))
    page_size = min(250, max(50, max_rows))
    offset = 0
    while enqueued < max_rows and scanned < scan_cap:
        attachments = base_query.offset(offset).limit(min(page_size, scan_cap - scanned)).all()
        if not attachments:
            break
        offset += len(attachments)
        for attachment in attachments:
            scanned += 1
            if not _attachment_is_image(attachment):
                skipped_non_image += 1
                continue
            dedupe_key = _media_transcription_key(attachment)
            if preferred_target_attachment_ids is not None and int(attachment.id) not in preferred_target_attachment_ids:
                skipped_duplicate_variants += 1
                existing = (
                    session.query(Job)
                    .filter(Job.kind == MEDIA_TRANSCRIPTION_JOB_KIND, Job.dedupe_key == dedupe_key)
                    .first()
                )
                if existing is not None and str(existing.status or "").strip().lower() in {"pending", "error"}:
                    now = utcnow()
                    payload = _json_dict(existing.payload)
                    payload["terminal_skip_reason"] = "duplicate_lower_resolution_media_variant"
                    payload["duplicate_variant_settled_at"] = _dt(now)
                    existing.status = "success"
                    existing.run_after = None
                    existing.locked_at = None
                    existing.finished_at = now
                    existing.last_error = "Skipped duplicate lower-resolution media variant."
                    existing.payload = payload
                    existing.updated_at = now
                    settled_duplicate_variants += 1
                continue
            low_value, _low_value_reason = _attachment_is_low_value_media_for_transcription(attachment)
            if low_value:
                skipped_low_value += 1
                continue
            doc = attachment.document
            has_prep_blocking_ocr = bool(
                doc is not None and _document_extra_has_prep_blocking_image_ocr_for_attachment(doc.extra, attachment)
            )
            if doc is not None and _document_has_image_ocr_for_attachment(doc, attachment):
                skipped_existing += 1
                continue
            existing = (
                session.query(Job)
                .filter(Job.kind == MEDIA_TRANSCRIPTION_JOB_KIND, Job.dedupe_key == dedupe_key)
                .first()
            )
            if existing is not None:
                status = str(existing.status or "").strip().lower()
                attempts = int(existing.attempts or 0)
                max_attempts = max(1, int(existing.max_attempts or 1))
                if has_prep_blocking_ocr and (status == "success" or (status == "error" and attempts >= max_attempts)):
                    payload = _json_dict(existing.payload)
                    now = utcnow()
                    payload["reopened_reason"] = "vlm_required_media_retry"
                    payload["reopened_at"] = _dt(now)
                    payload["force_vlm_for_prep_blocking_ocr"] = True
                    existing.status = "pending"
                    existing.run_after = None
                    existing.locked_at = None
                    existing.finished_at = None
                    existing.last_error = "VLM visual readout is required before media can enter signal extraction."
                    existing.max_attempts = max(max_attempts, attempts + 3)
                    existing.payload = payload
                    existing.updated_at = now
                    enqueued += 1
                    reopened_prep_blocking += 1
                    if enqueued >= max_rows:
                        break
                continue
            now = utcnow()
            session.add(
                Job(
                    kind=MEDIA_TRANSCRIPTION_JOB_KIND,
                    status="pending",
                    priority=5,
                    payload={
                        "document_id": int(attachment.document_id),
                        "attachment_id": int(attachment.id),
                        "sha256": attachment.sha256,
                        "original_url": attachment.original_url,
                        "content_type": attachment.content_type,
                    },
                    dedupe_key=dedupe_key,
                    attempts=0,
                    max_attempts=5,
                    run_after=None,
                    locked_at=None,
                    started_at=None,
                    finished_at=None,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            enqueued += 1
            if enqueued >= max_rows:
                break
    session.flush()
    return {
        "scanned": scanned,
        "enqueued": enqueued,
        "reopened_prep_blocking": reopened_prep_blocking,
        "settled_duplicate_variants": settled_duplicate_variants,
        "skipped_existing_ocr": skipped_existing,
        "skipped_duplicate_variants": skipped_duplicate_variants,
        "skipped_non_image": skipped_non_image,
        "skipped_low_value_media": skipped_low_value,
        "backfilled_documents": backfilled_documents,
        "backfilled_attachments": backfilled_attachments,
        "pdf_page_rendered_documents": pdf_page_rendered_documents,
        "pdf_page_rendered_attachments": pdf_page_rendered_attachments,
        "target_document_ids": target_document_ids,
    }


def requeue_media_transcription_jobs(
    session: Session,
    *,
    document_ids: list[int],
    reason: str,
    campaign_id: str | None = None,
    include_success: bool = False,
    additional_attempts: int = 3,
    limit: int = 1000,
    dry_run: bool = False,
) -> dict[str, Any]:
    target_document_ids = set(_int_list(document_ids)[:50])
    if not target_document_ids:
        raise ValueError("At least one document_id is required.")
    clean_reason = _clean(reason)
    if len(clean_reason) < 8:
        raise ValueError("A specific requeue reason of at least 8 characters is required.")
    clean_campaign_id = _clean(campaign_id)[:128] or None
    retry_increment = max(1, min(int(additional_attempts or 3), 10))
    max_rows = _limit(limit, default=1000, high=5000)
    accessible_document_ids = {
        int(row[0])
        for row in session.query(Document.id)
        .filter(Document.id.in_(sorted(target_document_ids)))
        .filter(agent_intake_visible_filter(session))
        .all()
    }
    inaccessible_document_ids = sorted(target_document_ids - accessible_document_ids)
    if not accessible_document_ids:
        raise ValueError("No requested documents are available to the local agent.")

    eligible_statuses = ["error", "success"] if include_success else ["error"]
    rows = (
        session.query(Job)
        .filter(Job.kind == MEDIA_TRANSCRIPTION_JOB_KIND, Job.status.in_(eligible_statuses))
        .order_by(Job.created_at.asc(), Job.id.asc())
        .limit(20_000)
        .all()
    )
    candidates = [job for job in rows if _media_job_document_id(job) in accessible_document_ids]
    attachment_ids = sorted(
        {attachment_id for job in candidates for attachment_id in [_media_job_attachment_id(job)] if attachment_id is not None}
    )
    attachments = {
        int(row.id): row
        for row in session.query(Attachment).filter(Attachment.id.in_(attachment_ids)).all()
    } if attachment_ids else {}
    documents = {
        int(row.id): row
        for row in session.query(Document)
        .filter(Document.id.in_(sorted(accessible_document_ids)))
        .all()
    }

    now = utcnow()
    requeued_job_ids: list[int] = []
    skipped_existing_ocr = 0
    skipped_orphaned = 0
    matched_error_jobs = 0
    for job in candidates:
        if len(requeued_job_ids) >= max_rows:
            break
        matched_error_jobs += 1
        document_id = _media_job_document_id(job)
        attachment_id = _media_job_attachment_id(job)
        attachment = attachments.get(int(attachment_id)) if attachment_id is not None else None
        document = documents.get(int(document_id)) if document_id is not None else None
        if (
            attachment is None
            or document is None
            or int(attachment.document_id) != int(document.id)
        ):
            skipped_orphaned += 1
            continue
        if not include_success and _document_has_image_ocr_for_attachment(document, attachment):
            skipped_existing_ocr += 1
            continue
        requeued_job_ids.append(int(job.id))
        if dry_run:
            continue

        payload = _json_dict(job.payload)
        history = list(payload.get("requeue_history") or [])
        history.append(
            {
                "at": _dt(now),
                "reason": clean_reason,
                "campaign_id": clean_campaign_id,
                "prior_status": str(job.status or ""),
                "prior_attempts": int(job.attempts or 0),
                "prior_max_attempts": int(job.max_attempts or 0),
                "prior_error": _shorten(job.last_error, 1000) if job.last_error else None,
            }
        )
        payload["requeue_history"] = history[-5:]
        payload["requeued_at"] = _dt(now)
        payload["requeued_reason"] = clean_reason
        if clean_campaign_id:
            payload["backfill_campaign_id"] = clean_campaign_id
        if include_success:
            payload["backfill_force_reprocess"] = True
        attempts = int(job.attempts or 0)
        job.status = "pending"
        job.max_attempts = max(int(job.max_attempts or 1), attempts + retry_increment)
        job.run_after = None
        job.locked_at = None
        job.started_at = None
        job.finished_at = None
        job.payload = payload
        job.updated_at = now

    if requeued_job_ids and not dry_run:
        session.flush()
    return {
        "dry_run": bool(dry_run),
        "campaign_id": clean_campaign_id,
        "include_success": bool(include_success),
        "reason": clean_reason,
        "target_document_ids": sorted(target_document_ids),
        "accessible_document_ids": sorted(accessible_document_ids),
        "inaccessible_document_ids": inaccessible_document_ids,
        "matched_error_jobs": matched_error_jobs,
        "requeued_jobs": len(requeued_job_ids),
        "requeued_job_ids": requeued_job_ids[:200],
        "skipped_existing_ocr": skipped_existing_ocr,
        "skipped_orphaned": skipped_orphaned,
        "limit_reached": len(requeued_job_ids) >= max_rows and len(candidates) > len(requeued_job_ids),
        "additional_attempts": retry_increment,
    }


def _document_has_unresolved_media_transcription_work(session: Session, doc: Document) -> bool:
    if not _agent_can_access_document(session, int(doc.id)):
        return False
    image_attachments = _preferred_media_attachments(
        [attachment for attachment in list(doc.attachments or []) if _attachment_is_image(attachment)]
    )
    if not image_attachments:
        return False
    for attachment in image_attachments:
        if _document_has_image_ocr_for_attachment(doc, attachment):
            continue
        return True
    return False


def _defer_media_pending_documents(session: Session, docs: list[Document], *, limit: int) -> tuple[list[Document], int]:
    out: list[Document] = []
    deferred = 0
    for doc in docs:
        if _document_has_unresolved_media_transcription_work(session, doc):
            deferred += 1
            continue
        out.append(doc)
        if len(out) >= limit:
            break
    return out, deferred


def _defer_media_pending_document_plan_rows(
    session: Session,
    rows: list[dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], int, list[int]]:
    doc_ids = [int(row["id"]) for row in rows if row.get("id") is not None]
    if not doc_ids:
        return [], 0, []
    attachments = (
        session.query(Attachment)
        .options(
            load_only(
                Attachment.id,
                Attachment.document_id,
                Attachment.kind,
                Attachment.content_type,
                Attachment.object_path,
                Attachment.original_url,
                Attachment.sha256,
            )
        )
        .filter(Attachment.document_id.in_(doc_ids))
        .all()
    )
    image_attachments_by_doc: dict[int, list[Attachment]] = {}
    for attachment in attachments:
        if not _attachment_is_image(attachment):
            continue
        image_attachments_by_doc.setdefault(int(attachment.document_id), []).append(attachment)
    image_attachments_by_doc = {
        doc_id: _preferred_media_attachments(rows)
        for doc_id, rows in image_attachments_by_doc.items()
    }
    if not image_attachments_by_doc:
        return rows[:limit], 0, []

    extra_by_doc = {
        int(doc_id): extra
        for doc_id, extra in session.query(Document.id, Document.extra)
        .filter(Document.id.in_(sorted(image_attachments_by_doc)))
        .all()
    }

    def has_unresolved_media(doc_id: int) -> bool:
        extra_payload = extra_by_doc.get(int(doc_id), {})
        for attachment in image_attachments_by_doc.get(int(doc_id), []):
            if _document_extra_has_image_ocr_for_attachment(extra_payload, attachment):
                continue
            return True
        return False

    out: list[dict[str, Any]] = []
    deferred = 0
    deferred_ids: list[int] = []
    for row in rows:
        doc_id = int(row["id"])
        if has_unresolved_media(doc_id):
            deferred += 1
            if doc_id not in deferred_ids:
                deferred_ids.append(doc_id)
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out, deferred, deferred_ids


def _upsert_document_stage_state(
    session: Session,
    *,
    document_id: int,
    stage: str,
    status: str | None = None,
    run_id: int | None = None,
    prompt_version: str | None = None,
    model_version: str | None = None,
    failure_delta: int = 0,
    quarantine_stage: str | None = None,
    quarantine_reason: str | None = None,
    terminal_skip_reason: str | None = None,
    payload: dict[str, Any] | None = None,
    media_pending_count: int | None = None,
    media_done_count: int | None = None,
    non_signal_media_count: int | None = None,
) -> LocalAgentDocumentStageState:
    now = utcnow()
    row = (
        session.query(LocalAgentDocumentStageState)
        .filter(LocalAgentDocumentStageState.document_id == int(document_id))
        .first()
    )
    if row is None:
        row = LocalAgentDocumentStageState(
            document_id=int(document_id),
            created_at=now,
            updated_at=now,
            status_payload={},
        )
        session.add(row)
    stage_key = str(stage or "").strip().lower()[:32] or "unknown"
    status_value = str(status or "").strip().lower()[:32] or None
    field_by_stage = {
        "prep": "prep_status",
        "hydration": "prep_status",
        "read": "prep_status",
        "ocr": "ocr_status",
        "vlm": "vlm_status",
        "translation": "translation_status",
        "signal": "signal_status",
        "signal_extraction": "signal_status",
        "durable": "durable_status",
        "durable_reasoning": "durable_status",
        "quarantine": "prep_status",
    }
    status_field = field_by_stage.get(stage_key)
    if status_field and status_value:
        setattr(row, status_field, status_value)
    row.last_stage = stage_key
    row.last_attempt_at = now
    if prompt_version:
        row.prompt_version = str(prompt_version or "").strip()[:64]
    if model_version:
        row.model_version = str(model_version or "").strip()[:255]
    if media_pending_count is not None:
        row.media_pending_count = max(0, int(media_pending_count or 0))
    if media_done_count is not None:
        row.media_done_count = max(0, int(media_done_count or 0))
    if non_signal_media_count is not None:
        row.non_signal_media_count = max(0, int(non_signal_media_count or 0))
    if failure_delta:
        row.failure_count = max(0, int(row.failure_count or 0) + int(failure_delta))
    if quarantine_stage:
        row.quarantine_stage = str(quarantine_stage or "").strip()[:32]
        row.quarantine_reason = _shorten(quarantine_reason or "quarantined", 1000)
        row.quarantined_at = now
    if terminal_skip_reason:
        row.terminal_skip_reason = _shorten(terminal_skip_reason, 1000)
    current_payload = _json_dict(row.status_payload)
    update_payload = dict(payload or {})
    if run_id is not None:
        update_payload["run_id"] = int(run_id)
    update_payload["updated_at"] = _dt(now)
    row.status_payload = {**current_payload, **update_payload}
    row.updated_at = now
    session.flush()
    return row


def _active_window_document_ids(
    session: Session,
    *,
    lookback_hours: int | None,
    prompt_version: str | None,
    include_filings: bool = False,
) -> set[int]:
    rows, _hours, _cutoff, _priority = _agent_queue_scope(
        session,
        lookback_hours=lookback_hours,
        include_filings=include_filings,
        include_opened=False,
        include_product_snapshots=False,
        prompt_version=prompt_version,
    )
    return {
        int(row[0])
        for row in rows.with_entities(Document.id).limit(20_000).all()
        if row[0] is not None
    }


def _media_job_document_id(job: Job) -> int | None:
    payload = _json_dict(job.payload)
    value = payload.get("document_id")
    return int(value) if str(value or "").isdigit() else None


def _media_job_attachment_id(job: Job) -> int | None:
    payload = _json_dict(job.payload)
    value = payload.get("attachment_id")
    return int(value) if str(value or "").isdigit() else None


def _media_stage_job_counts(
    session: Session,
    *,
    active_document_ids: set[int] | None = None,
) -> dict[str, int]:
    now = utcnow()
    rows = (
        session.query(Job)
        .filter(Job.kind == MEDIA_TRANSCRIPTION_JOB_KIND)
        .filter(Job.status.in_(["pending", "running", "error"]))
        .order_by(Job.created_at.asc(), Job.id.asc())
        .limit(20_000)
        .all()
    )
    attachment_ids = sorted({value for job in rows for value in [_media_job_attachment_id(job)] if value is not None})
    attachments = {
        int(row.id): row
        for row in session.query(Attachment).filter(Attachment.id.in_(attachment_ids)).all()
    } if attachment_ids else {}
    document_ids = sorted(
        {
            int(attachment.document_id)
            for attachment in attachments.values()
            if attachment.document_id is not None
        }
        | {value for job in rows for value in [_media_job_document_id(job)] if value is not None}
    )
    extra_by_doc = {
        int(doc_id): extra
        for doc_id, extra in session.query(Document.id, Document.extra).filter(Document.id.in_(document_ids)).all()
    } if document_ids else {}
    counts = {
        "pending_jobs": 0,
        "running_jobs": 0,
        "error_jobs": 0,
        "retryable_error_jobs": 0,
        "retry_delayed_error_jobs": 0,
        "exhausted_error_jobs": 0,
        "ocr_pending_jobs": 0,
        "vlm_pending_jobs": 0,
        "quarantined_jobs": 0,
    }
    for job in rows:
        status = str(job.status or "").strip().lower()
        doc_id = _media_job_document_id(job)
        attachment_id = _media_job_attachment_id(job)
        attachment = attachments.get(int(attachment_id)) if attachment_id is not None else None
        if attachment is not None:
            doc_id = int(attachment.document_id)
        if active_document_ids is not None and (doc_id is None or int(doc_id) not in active_document_ids):
            continue
        attempts = int(job.attempts or 0)
        max_attempts = max(1, int(job.max_attempts or 1))
        if status == "running":
            counts["running_jobs"] += 1
        elif status == "error":
            counts["error_jobs"] += 1
            if attempts < max_attempts:
                if job.run_after is None or job.run_after <= now:
                    counts["retryable_error_jobs"] += 1
                else:
                    counts["retry_delayed_error_jobs"] += 1
                    continue
            else:
                counts["exhausted_error_jobs"] += 1
                continue
        elif status == "pending":
            counts["pending_jobs"] += 1
        else:
            continue

        if status == "error" and attempts >= max_attempts:
            continue
        if attachment is None:
            counts["ocr_pending_jobs"] += 1
            continue
        extra_payload = extra_by_doc.get(int(doc_id or 0), {})
        payload = _json_dict(job.payload)
        force_vlm = _truthy_flag(payload.get("force_vlm_after_ocr_failure")) is True
        force_vlm = force_vlm or _truthy_flag(payload.get("force_vlm_for_prep_blocking_ocr")) is True
        if force_vlm or _document_extra_has_prep_blocking_image_ocr_for_attachment(extra_payload, attachment):
            counts["vlm_pending_jobs"] += 1
        elif not _document_extra_has_image_ocr_for_attachment(extra_payload, attachment):
            counts["ocr_pending_jobs"] += 1
    counts["actionable_jobs"] = counts["ocr_pending_jobs"] + counts["vlm_pending_jobs"]
    return counts


def _source_config_url(source: Source | None) -> str | None:
    if source is None:
        return None
    config = _json_dict(getattr(source, "config", None))
    for key in ("url", "source_url", "feed_url", "base_url"):
        value = str(config.get(key) or "").strip()
        if value:
            return value
    return None


def serialize_media_transcription_job(job: Job, attachment: Attachment, doc: Document | None = None) -> dict[str, Any]:
    source = doc.source if doc is not None else None
    pdf_page_source = _pdf_page_attachment_source(attachment)
    payload = _json_dict(job.payload)
    return {
        "job_id": int(job.id),
        "document_id": int(attachment.document_id),
        "attachment_id": int(attachment.id),
        "document_title": str(doc.title or "").strip() if doc is not None else None,
        "document_url": str((doc.url or doc.canonical_url) or "").strip() if doc is not None else None,
        "canonical_url": str(doc.canonical_url or "").strip() if doc is not None else None,
        "source_name": str(source.name or "").strip() if source is not None else None,
        "source_url": _source_config_url(source),
        "content_type": attachment.content_type,
        "sha256": attachment.sha256,
        "original_url": attachment.original_url,
        "pdf_page_source": pdf_page_source,
        "bytes_url": f"/api/local-agent/media/attachments/{int(attachment.id)}/bytes",
        "attempts": int(job.attempts or 0),
        "max_attempts": int(job.max_attempts or 0),
        "force_vlm_after_ocr_failure": _truthy_flag(payload.get("force_vlm_after_ocr_failure")) is True,
        "force_vlm_for_prep_blocking_ocr": _truthy_flag(payload.get("force_vlm_for_prep_blocking_ocr")) is True,
    }


def _release_or_reroute_exhausted_media_jobs(session: Session, *, now: datetime) -> int:
    active_document_ids = _active_window_document_ids(
        session,
        lookback_hours=getattr(settings, "agent_lookback_hours", 48),
        prompt_version=None,
        include_filings=False,
    )
    if not active_document_ids:
        return 0
    rows = (
        session.query(Job)
        .filter(Job.kind == MEDIA_TRANSCRIPTION_JOB_KIND)
        .filter(Job.status == "error")
        .filter(Job.attempts >= Job.max_attempts)
        .order_by(Job.updated_at.asc(), Job.id.asc())
        .limit(100)
        .all()
    )
    changed = 0
    for job in rows:
        payload = _json_dict(job.payload)
        attachment_id = payload.get("attachment_id")
        document_id = payload.get("document_id")
        if not str(attachment_id or "").isdigit() or not str(document_id or "").isdigit():
            continue
        attachment = session.query(Attachment).filter_by(id=int(attachment_id)).first()
        doc = session.query(Document).filter_by(id=int(document_id)).first()
        if attachment is None or doc is None:
            continue
        if active_document_ids and int(doc.id) not in active_document_ids:
            continue
        has_prep_blocking_ocr = _document_extra_has_prep_blocking_image_ocr_for_attachment(doc.extra, attachment)
        if has_prep_blocking_ocr:
            if _truthy_flag(payload.get("terminal_vlm_error_recorded")) is True:
                continue
            payload["terminal_vlm_error_recorded"] = True
            payload["terminal_vlm_error_recorded_at"] = _dt(now)
            job.payload = payload
            job.status = "error"
            job.finished_at = job.finished_at or now
            job.locked_at = None
            job.run_after = None
            job.last_error = "VLM terminal failure; media remains unresolved and blocked from signal extraction."
            job.updated_at = now
            _upsert_document_stage_state(
                session,
                document_id=int(doc.id),
                stage="vlm",
                status="error",
                failure_delta=1,
                quarantine_reason="VLM terminal failure; media unresolved.",
                terminal_skip_reason="VLM terminal failure; media remains blocked from signal extraction.",
                payload={"job_id": int(job.id), "attachment_id": int(attachment.id), "media_unresolved": True},
            )
            session.add(
                DocumentEvent(
                    document_id=int(doc.id),
                    event_type=MEDIA_TRANSCRIPTION_EVENT_TYPE,
                    event_at=now,
                    meta={
                        "job_id": int(job.id),
                        "attachment_id": int(attachment.id),
                        "status": "terminal_media_error",
                        "last_error": job.last_error,
                    },
                )
            )
            changed += 1
            continue

        if _truthy_flag(payload.get("force_vlm_after_ocr_failure")) is True:
            if _truthy_flag(payload.get("terminal_vlm_error_recorded")) is True:
                continue
            payload["terminal_vlm_error_recorded"] = True
            payload["terminal_vlm_error_recorded_at"] = _dt(now)
            job.payload = payload
            job.status = "error"
            job.finished_at = job.finished_at or now
            job.locked_at = None
            job.run_after = None
            job.last_error = "VLM retry budget exhausted after OCR failure; media remains unresolved and blocked from signal extraction."
            job.updated_at = now
            _upsert_document_stage_state(
                session,
                document_id=int(doc.id),
                stage="vlm",
                status="error",
                failure_delta=1,
                quarantine_reason="VLM terminal failure after OCR failure.",
                terminal_skip_reason="VLM retry budget exhausted; media remains blocked from signal extraction.",
                payload={"job_id": int(job.id), "attachment_id": int(attachment.id), "media_unresolved": True},
            )
            session.add(
                DocumentEvent(
                    document_id=int(doc.id),
                    event_type=MEDIA_TRANSCRIPTION_EVENT_TYPE,
                    event_at=now,
                    meta={
                        "job_id": int(job.id),
                        "attachment_id": int(attachment.id),
                        "status": "terminal_media_error",
                        "last_error": job.last_error,
                    },
                )
            )
            changed += 1
            continue

        payload["force_vlm_after_ocr_failure"] = True
        payload["ocr_failure_exhausted_at"] = _dt(now)
        job.payload = payload
        job.status = "pending"
        job.run_after = None
        job.locked_at = None
        job.finished_at = None
        job.max_attempts = max(int(job.max_attempts or 1), int(job.attempts or 0) + 3)
        job.last_error = "OCR retry budget exhausted; routing media to VLM visual readout."
        job.updated_at = now
        _upsert_document_stage_state(
            session,
            document_id=int(doc.id),
            stage="ocr",
            status="rerouted_to_vlm",
            failure_delta=1,
            payload={"job_id": int(job.id), "attachment_id": int(attachment.id), "force_vlm_after_ocr_failure": True},
        )
        changed += 1
    if changed:
        session.flush()
    return changed


def claim_media_transcription_jobs(
    session: Session,
    *,
    run_id: int | None = None,
    limit: int = 4,
    stale_after_minutes: int = 60,
    document_ids: list[int] | None = None,
    media_lane: str | None = None,
) -> list[dict[str, Any]]:
    max_rows = _limit(limit, default=4, high=50)
    target_document_ids = set(_int_list(document_ids)[:50])
    lane = str(media_lane or "any").strip().lower().replace("-", "_")
    if lane in {"vision", "vision_only", "vlm_only", "vlm_first"}:
        lane = "vlm"
    elif lane in {"ocr_only", "ocr_first"}:
        lane = "ocr"
    elif lane not in {"any", "ocr", "vlm"}:
        lane = "any"
    now = utcnow()
    _release_or_reroute_exhausted_media_jobs(session, now=now)
    stale_cutoff = now - timedelta(minutes=max(5, min(int(stale_after_minutes or 60), 24 * 60)))
    if target_document_ids:
        candidate_limit = min(20_000, max(1_000, max_rows * 1_000))
    elif lane != "any":
        candidate_limit = min(20_000, max(1_000, max_rows * 100))
    else:
        candidate_limit = max_rows
    eligible = (
        session.query(Job)
        .filter(Job.kind == MEDIA_TRANSCRIPTION_JOB_KIND)
        .filter(
            or_(
                Job.status == "pending",
                and_(Job.status == "error", Job.attempts < Job.max_attempts, or_(Job.run_after.is_(None), Job.run_after <= now)),
                and_(Job.status == "running", Job.locked_at.is_not(None), Job.locked_at < stale_cutoff),
            )
        )
        .order_by(Job.priority.asc(), Job.created_at.asc(), Job.id.asc())
        .limit(candidate_limit)
        .all()
    )
    rows: list[dict[str, Any]] = []
    for job in eligible:
        if len(rows) >= max_rows:
            break
        payload = _json_dict(job.payload)
        attachment_id = payload.get("attachment_id")
        if not str(attachment_id or "").isdigit():
            job.status = "error"
            job.last_error = "media transcription job missing attachment_id"
            job.finished_at = now
            job.updated_at = now
            continue
        attachment = session.query(Attachment).filter_by(id=int(attachment_id)).first()
        if attachment is None or not _attachment_is_image(attachment):
            job.status = "error"
            job.last_error = "media transcription attachment missing or not image"
            job.finished_at = now
            job.updated_at = now
            continue
        low_value, low_value_reason = _attachment_is_low_value_media_for_transcription(attachment)
        if low_value:
            payload["skipped_reason"] = low_value_reason or "low_value_media"
            payload["skipped_at"] = _dt(now)
            job.payload = payload
            job.status = "success"
            job.last_error = None
            job.finished_at = now
            job.updated_at = now
            continue
        if target_document_ids and int(attachment.document_id) not in target_document_ids:
            continue
        doc = session.query(Document).filter_by(id=int(attachment.document_id)).first()
        if doc is not None and not _agent_can_access_document(session, int(doc.id)):
            payload["skipped_reason"] = "restricted_document_outside_active_universe"
            payload["skipped_at"] = _dt(now)
            job.payload = payload
            job.status = "success"
            job.finished_at = now
            job.updated_at = now
            continue
        if (
            doc is not None
            and _document_has_image_ocr_for_attachment(doc, attachment)
            and _truthy_flag(payload.get("backfill_force_reprocess")) is not True
        ):
            job.status = "success"
            job.finished_at = now
            job.updated_at = now
            continue
        requires_vlm = bool(
            _truthy_flag(payload.get("force_vlm_after_ocr_failure")) is True
            or _truthy_flag(payload.get("force_vlm_for_prep_blocking_ocr")) is True
            or (doc is not None and _document_extra_has_prep_blocking_image_ocr_for_attachment(doc.extra, attachment))
        )
        if lane == "ocr" and requires_vlm:
            continue
        if lane == "vlm" and not requires_vlm:
            continue
        if doc is not None and _document_extra_has_prep_blocking_image_ocr_for_attachment(doc.extra, attachment):
            payload["force_vlm_for_prep_blocking_ocr"] = True
        job.status = "running"
        job.locked_at = now
        job.started_at = job.started_at or now
        job.updated_at = now
        job.attempts = int(job.attempts or 0) + 1
        payload["claimed_run_id"] = int(run_id) if run_id is not None else None
        payload["claimed_at"] = _dt(now)
        job.payload = payload
        rows.append(serialize_media_transcription_job(job, attachment, doc))
    session.flush()
    return rows


def media_attachment_file(session: Session, *, attachment_id: int) -> tuple[Attachment, Any]:
    attachment = session.query(Attachment).filter_by(id=int(attachment_id)).first()
    if attachment is None:
        raise FileNotFoundError("Media attachment not found.")
    if not _attachment_is_image(attachment):
        raise ValueError("Attachment is not image media.")
    if not _agent_can_access_document(session, int(attachment.document_id)):
        raise PermissionError("Media attachment document is outside local-agent scope.")
    path = object_abs_path(str(attachment.object_path or ""))
    root = settings.objects_dir.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Attachment object path escapes object store.") from exc
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError("Media attachment file is missing.")
    return attachment, resolved


MEDIA_READY_RETRY_STATUS = "retry_pending"
MEDIA_READY_RETRY_REASON = "media_transcription_ready_after_error"
MEDIA_SIGNAL_READY_RETRY_REASON = "media_transcription_ready_after_prior_signal"
MEDIA_TERMINAL_NON_SIGNAL_FAILURE_TYPES = {
    "media_object_missing",
    "media_object_not_image",
    "media_object_corrupt",
    "media_object_empty",
}


def _complete_terminal_non_signal_media_failure(
    session: Session,
    *,
    job: Job,
    attachment: Attachment,
    doc: Document,
    failure_type: str,
    error: str,
    run_id: int | None,
    now: datetime,
) -> dict[str, Any]:
    extra = _json_dict(doc.extra)
    rows = [dict(row) for row in _json_list(extra.get("image_ocr")) if isinstance(row, dict)]
    detail = {
        "terminal_failure_type": failure_type,
        "terminal_failure_reason": error,
        "discarded_by": "local_agent_media_prep",
    }
    new_row = {
        "attachment_id": int(attachment.id),
        "backend": "media-object-validation",
        "model": None,
        "original_url": str(attachment.original_url or "").strip() or None,
        "object_path": str(attachment.object_path or "").strip() or None,
        "sha256": str(attachment.sha256 or "").strip() or None,
        "text": "",
        "text_en": None,
        "summary": f"Media discarded before OCR/VLM: {failure_type}.",
        "content_type": attachment.content_type,
        "confidence": "high",
        "media_kind": "invalid_or_missing_media",
        "ocr_quality": "unavailable",
        "needs_vlm": False,
        "vlm_required": False,
        "vlm_available": False,
        "vlm_required_reasons": [],
        "prep_blocking": False,
        "ocr_signal_ready": False,
        "source_risk": "invalid_media_object",
        "visual_signal_terms": [],
        "layout_labels": [],
        "structured_series": [],
        "signal": "non_signal",
        "is_signal": False,
        "signal_reason": "Media object was missing, non-image, empty, or corrupt; it cannot carry usable visual signal.",
        "translation": {},
        "detail": detail,
        "created_at": _dt(now),
    }
    rows, replaced = _upsert_image_ocr_row(rows, attachment=attachment, new_row=new_row)
    extra["image_ocr"] = rows
    extra["image_ocr_status"] = {
        "available": True,
        "backend": "media-object-validation",
        "model": None,
        "available_backends": ["media-object-validation"],
        "detail": "terminal non-signal media object failure",
        "updated_at": _dt(now),
    }
    doc.extra = extra

    media_pending_count = sum(1 for row in rows if _image_ocr_row_is_prep_blocking(row))
    media_done_count = sum(1 for row in rows if not _image_ocr_row_is_prep_blocking(row))
    non_signal_media_count = sum(1 for row in rows if _image_ocr_row_signal(row).get("is_signal") is False)
    _upsert_document_stage_state(
        session,
        document_id=int(doc.id),
        stage="media_object_validation",
        status="discarded_non_signal",
        run_id=run_id,
        media_pending_count=media_pending_count,
        media_done_count=media_done_count,
        non_signal_media_count=non_signal_media_count,
        payload={
            "job_id": int(job.id),
            "attachment_id": int(attachment.id),
            "failure_type": failure_type,
        },
    )
    job.status = "success"
    job.finished_at = now
    job.locked_at = None
    job.run_after = None
    job.last_error = None
    job.updated_at = now
    job.payload = {
        **_json_dict(job.payload),
        "completed_run_id": int(run_id) if run_id is not None else None,
        "completed_at": _dt(now),
        "terminal_failure_type": failure_type,
        "prep_blocking": False,
        "force_vlm_for_prep_blocking_ocr": False,
    }
    retry_pending_compressions = _mark_media_ready_compression_retries(
        session,
        document_id=int(doc.id),
        now=now,
        media_signal=False,
    )
    session.add(
        DocumentEvent(
            document_id=int(doc.id),
            event_type=MEDIA_TRANSCRIPTION_EVENT_TYPE,
            event_at=now,
            meta={
                "job_id": int(job.id),
                "attachment_id": int(attachment.id),
                "sha256": attachment.sha256,
                "backend": "media-object-validation",
                "failure_type": failure_type,
                "media_signal": "non_signal",
                "prep_blocking": False,
                "terminal_non_signal_failure": True,
                "retry_pending_compressions": retry_pending_compressions,
            },
        )
    )
    session.flush()
    return {
        "status": "success",
        "job_id": int(job.id),
        "document_id": int(doc.id),
        "attachment_id": int(attachment.id),
        "failure_type": failure_type,
        "media_signal": "non_signal",
        "terminal_non_signal_failure": True,
        "retry_pending_compressions": retry_pending_compressions,
    }


def _compression_looks_metadata_only(row: LocalAgentDocumentCompression) -> bool:
    text = _clean(" ".join([row.summary_short or "", row.skip_reason or "", row.error or ""])).lower()
    if not text:
        return False
    patterns = (
        "metadata only",
        "metadata record",
        "no content provided",
        "does not contain the actual",
        "without the content",
        "no substantive information",
        "url to the transcript",
        "transcript text",
    )
    return any(pattern in text for pattern in patterns)


def _mark_text_ready_compression_retries(
    session: Session,
    *,
    document_id: int,
    now: datetime,
    reason: str = TEXT_READY_RETRY_REASON,
) -> int:
    existing_terminal = (
        session.query(LocalAgentDocumentCompression.id)
        .filter(LocalAgentDocumentCompression.document_id == int(document_id))
        .filter(LocalAgentDocumentCompression.status.in_(["compressed", "attached", "written"]))
        .first()
    )
    if existing_terminal is not None:
        return 0
    rows = (
        session.query(LocalAgentDocumentCompression)
        .filter(LocalAgentDocumentCompression.document_id == int(document_id))
        .filter(LocalAgentDocumentCompression.status.in_(["error", "skipped", "noop"]))
        .all()
    )
    updated = 0
    for row in rows:
        if row.status != "error" and not _compression_looks_metadata_only(row):
            continue
        row.status = TEXT_READY_RETRY_STATUS
        row.skip_reason = reason
        row.summary_short = "Document text is now available; compression retry pending."
        row.updated_at = now
        updated += 1
    return updated


def _mark_media_ready_compression_retries(session: Session, *, document_id: int, now: datetime, media_signal: bool = False) -> int:
    terminal_rows = (
        session.query(LocalAgentDocumentCompression)
        .filter(LocalAgentDocumentCompression.document_id == int(document_id))
        .filter(LocalAgentDocumentCompression.status.in_(["compressed", "attached", "written", "skipped", "noop"]))
        .order_by(LocalAgentDocumentCompression.updated_at.desc(), LocalAgentDocumentCompression.id.desc())
        .all()
    )
    if terminal_rows:
        if media_signal:
            for row in terminal_rows:
                row.status = MEDIA_READY_RETRY_STATUS
                row.skip_reason = MEDIA_SIGNAL_READY_RETRY_REASON
                row.summary_short = "Media transcription added signal text; signal extraction retry pending."
                row.updated_at = now
            return len(terminal_rows)
        return 0
    rows = (
        session.query(LocalAgentDocumentCompression)
        .filter(LocalAgentDocumentCompression.document_id == int(document_id))
        .filter(LocalAgentDocumentCompression.status == "error")
        .all()
    )
    for row in rows:
        row.status = MEDIA_READY_RETRY_STATUS
        row.skip_reason = MEDIA_READY_RETRY_REASON
        row.summary_short = "Media transcription completed; compression retry pending."
        row.updated_at = now
    return len(rows)


def complete_media_transcription(session: Session, payload: dict[str, Any], *, run_id: int | None = None) -> dict[str, Any]:
    job_id = payload.get("job_id")
    attachment_id = payload.get("attachment_id")
    document_id = payload.get("document_id")
    if not str(job_id or "").isdigit() or not str(attachment_id or "").isdigit() or not str(document_id or "").isdigit():
        raise ValueError("job_id, attachment_id, and document_id are required.")
    job = session.query(Job).filter_by(id=int(job_id), kind=MEDIA_TRANSCRIPTION_JOB_KIND).first()
    if job is None:
        raise ValueError("Media transcription job not found.")
    attachment = session.query(Attachment).filter_by(id=int(attachment_id)).first()
    if attachment is None:
        raise ValueError("Media transcription attachment not found.")
    if int(attachment.document_id) != int(document_id):
        raise ValueError("Attachment does not belong to the submitted document.")
    if int(_json_dict(job.payload).get("attachment_id") or attachment.id) != int(attachment.id):
        raise ValueError("Job does not match the submitted attachment.")
    doc = session.query(Document).filter_by(id=int(document_id)).first()
    if doc is None:
        raise ValueError("Media transcription document not found.")

    status = str(payload.get("status") or "success").strip().lower()
    now = utcnow()
    if status not in {"success", "error"}:
        raise ValueError("Media transcription status must be success or error.")
    if status == "error":
        error = _shorten(payload.get("error") or "media transcription failed", 1000)
        failure_type = re.sub(
            r"[^a-z0-9_]+",
            "_",
            str(payload.get("failure_type") or payload.get("error_type") or "media_transcription_error").strip().lower(),
        ).strip("_")[:96] or "media_transcription_error"
        error_types = [str(value)[:96] for value in _json_list(payload.get("error_types")) if str(value or "").strip()]
        terminal_types = {failure_type, *error_types}
        if terminal_types.intersection(MEDIA_TERMINAL_NON_SIGNAL_FAILURE_TYPES):
            terminal_failure_type = sorted(terminal_types.intersection(MEDIA_TERMINAL_NON_SIGNAL_FAILURE_TYPES))[0]
            return _complete_terminal_non_signal_media_failure(
                session,
                job=job,
                attachment=attachment,
                doc=doc,
                failure_type=terminal_failure_type,
                error=error,
                run_id=run_id,
                now=now,
            )
        job.status = "error"
        job.last_error = error
        job.run_after = now + timedelta(minutes=min(60, 5 * max(1, int(job.attempts or 1))))
        job.updated_at = now
        job.payload = {
            **_json_dict(job.payload),
            "last_failure_type": failure_type,
            "last_error_types": error_types[:8],
            "last_error_at": _dt(now),
        }
        session.flush()
        return {"status": "error", "job_id": int(job.id), "failure_type": failure_type, "error_types": error_types[:8], "error": error}

    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("Media transcription text is required for success.")
    summary = str(payload.get("summary") or "").strip()
    backend = str(payload.get("backend") or "local-vlm").strip()[:64]
    model = str(payload.get("model") or "").strip()[:255] or None
    confidence = str(payload.get("confidence") or "").strip().lower()[:32] or None
    detail = _json_dict(payload.get("detail"))
    pdf_page_source = _pdf_page_attachment_source(attachment)
    if pdf_page_source:
        detail["pdf_page_source"] = pdf_page_source
    media_translation = _translate_media_text_for_agent(doc, text=text)
    if media_translation.get("needed"):
        detail["media_translation"] = {key: value for key, value in media_translation.items() if key != "text"}
    signal = _media_signal_assessment(text=text, summary=summary, detail=detail)
    assessment_detail = _json_dict(detail.get("ocr_assessment"))
    media_kind = str(detail.get("media_kind") or assessment_detail.get("media_kind") or "").strip()[:64] or None
    ocr_quality = str(detail.get("ocr_quality") or assessment_detail.get("ocr_quality") or "").strip()[:32] or None
    needs_vlm = _truthy_flag(detail.get("needs_vlm"))
    if needs_vlm is None:
        needs_vlm = _truthy_flag(assessment_detail.get("needs_vlm"))
    vlm_required = _truthy_flag(detail.get("vlm_required"))
    if vlm_required is None:
        vlm_required = needs_vlm
    vlm_available = _truthy_flag(detail.get("vlm_available"))
    if vlm_available is None:
        backend_marker = f"{backend} {model or ''}".lower()
        vlm_available = bool(_json_dict(detail.get("vision")) or "vlm" in backend_marker or "vision" in backend_marker)
    ocr_signal_ready = _media_ocr_text_is_signal_ready(text=text, summary=summary, ocr_quality=ocr_quality)
    ocr_only_complete = bool(
        ocr_signal_ready
        and str(media_kind or "").strip().lower() in OCR_ONLY_COMPLETE_MEDIA_KINDS
    )
    if ocr_only_complete and signal.get("is_signal") is not False and vlm_required is True and not vlm_available:
        vlm_required = False
    prep_blocking = _truthy_flag(detail.get("prep_blocking"))
    if prep_blocking is None:
        prep_blocking = bool(vlm_required is True and not vlm_available and signal.get("is_signal") is not False)
    elif prep_blocking and ocr_only_complete and signal.get("is_signal") is not False:
        prep_blocking = False
    source_risk = str(detail.get("source_risk") or assessment_detail.get("source_risk") or "").strip()[:64] or None
    visual_signal_terms = _json_list(detail.get("visual_signal_terms") or assessment_detail.get("visual_signal_terms"))
    layout_labels = _json_list(detail.get("layout_labels") or assessment_detail.get("layout_labels"))
    vlm_required_reasons = _json_list(detail.get("vlm_required_reasons"))
    structured_series = _normalize_media_structured_series(
        payload.get("structured_series")
        or detail.get("structured_series")
        or detail.get("chart_series")
        or detail.get("data_points")
        or _json_dict(detail.get("vision")).get("structured_series")
    )
    extra = _json_dict(doc.extra)
    rows = [dict(row) for row in _json_list(extra.get("image_ocr")) if isinstance(row, dict)]
    new_row = {
        "attachment_id": int(attachment.id),
        "backend": backend,
        "model": model,
        "original_url": str(attachment.original_url or "").strip() or None,
        "object_path": str(attachment.object_path or "").strip() or None,
        "sha256": str(attachment.sha256 or "").strip() or None,
        "text": text,
        "text_en": media_translation.get("text") if media_translation.get("available") else None,
        "summary": summary or _shorten(text, 600),
        "content_type": attachment.content_type,
        "confidence": confidence,
        "media_kind": media_kind,
        "ocr_quality": ocr_quality,
        "needs_vlm": needs_vlm,
        "vlm_required": vlm_required,
        "vlm_available": vlm_available,
        "vlm_required_reasons": [str(value) for value in vlm_required_reasons[:8]],
        "prep_blocking": bool(prep_blocking),
        "ocr_signal_ready": bool(ocr_signal_ready),
        "source_risk": source_risk,
        "visual_signal_terms": [str(value) for value in visual_signal_terms[:40]],
        "layout_labels": [str(value) for value in layout_labels[:40]],
        "structured_series": structured_series,
        "signal": signal.get("signal"),
        "is_signal": bool(signal.get("is_signal")),
        "signal_reason": signal.get("reason"),
        "translation": {key: value for key, value in media_translation.items() if key != "text"},
        "detail": detail,
        "created_at": _dt(now),
    }
    incoming_variant_key = _media_transcription_variant_key(new_row)
    media_variant_changed = not any(
        _image_ocr_row_matches(row, attachment)
        and (
            _media_transcription_variant_key(row) == incoming_variant_key
            or any(
                isinstance(variant, dict) and _media_transcription_variant_key(variant) == incoming_variant_key
                for variant in _json_list(row.get("variants"))
            )
        )
        for row in rows
    )
    rows, replaced = _upsert_image_ocr_row(rows, attachment=attachment, new_row=new_row)
    extra["image_ocr"] = rows
    extra["image_ocr_status"] = {
        "available": True,
        "backend": backend,
        "model": model,
        "available_backends": [backend],
        "detail": "local-agent media transcription",
        "updated_at": _dt(now),
    }
    doc.extra = extra

    media_pending_count = sum(1 for row in rows if _image_ocr_row_is_prep_blocking(row))
    media_done_count = sum(1 for row in rows if not _image_ocr_row_is_prep_blocking(row))
    non_signal_media_count = sum(1 for row in rows if _image_ocr_row_signal(row).get("is_signal") is False)
    if "ocr" in f"{backend} {model or ''}".lower() or ocr_quality:
        _upsert_document_stage_state(
            session,
            document_id=int(doc.id),
            stage="ocr",
            status="success",
            run_id=run_id,
            model_version=model,
            media_pending_count=media_pending_count,
            media_done_count=media_done_count,
            non_signal_media_count=non_signal_media_count,
            payload={
                "attachment_id": int(attachment.id),
                "backend": backend,
                "media_kind": media_kind,
                "ocr_quality": ocr_quality,
                "prep_blocking": bool(prep_blocking),
            },
        )
    if vlm_required or vlm_available or "vlm" in f"{backend} {model or ''}".lower() or "vision" in f"{backend} {model or ''}".lower():
        _upsert_document_stage_state(
            session,
            document_id=int(doc.id),
            stage="vlm",
            status="success" if vlm_available and not prep_blocking else "pending",
            run_id=run_id,
            model_version=model,
            media_pending_count=media_pending_count,
            media_done_count=media_done_count,
            non_signal_media_count=non_signal_media_count,
            payload={
                "attachment_id": int(attachment.id),
                "backend": backend,
                "vlm_required": bool(vlm_required),
                "vlm_available": bool(vlm_available),
                "prep_blocking": bool(prep_blocking),
                "vlm_required_reasons": [str(value) for value in vlm_required_reasons[:8]],
            },
        )

    if prep_blocking:
        job.status = "pending"
        job.finished_at = None
        job.last_error = "VLM visual readout is queued before media can enter signal extraction."
        job.run_after = None
    else:
        job.status = "success"
        job.finished_at = now
        job.last_error = None
        job.run_after = None
    job.locked_at = None
    job.updated_at = now
    job.payload = {
        **_json_dict(job.payload),
        "completed_run_id": int(run_id) if run_id is not None else None,
        "completed_at": _dt(now),
        "prep_blocking": bool(prep_blocking),
        "force_vlm_for_prep_blocking_ocr": bool(prep_blocking),
    }
    retry_pending_compressions = 0
    if not prep_blocking and media_variant_changed:
        retry_pending_compressions = _mark_media_ready_compression_retries(
            session,
            document_id=int(doc.id),
            now=now,
            media_signal=bool(signal.get("is_signal")),
        )
    session.add(
        DocumentEvent(
            document_id=int(doc.id),
            event_type=MEDIA_TRANSCRIPTION_EVENT_TYPE,
            event_at=now,
            meta={
                "job_id": int(job.id),
                "attachment_id": int(attachment.id),
                "sha256": attachment.sha256,
                "backend": backend,
                "model": model,
                "text_chars": len(text),
                "media_kind": media_kind,
                "ocr_quality": ocr_quality,
                "needs_vlm": needs_vlm,
                "vlm_required": vlm_required,
                "vlm_available": vlm_available,
                "prep_blocking": bool(prep_blocking),
                "ocr_signal_ready": bool(ocr_signal_ready),
                "media_signal": signal.get("signal"),
                "structured_series_rows": len(structured_series),
                "replaced": replaced,
                "retry_pending_compressions": retry_pending_compressions,
            },
        )
    )
    session.flush()
    return {
        "status": "retry_pending" if prep_blocking else "success",
        "job_id": int(job.id),
        "document_id": int(doc.id),
        "attachment_id": int(attachment.id),
        "text_chars": len(text),
        "media_signal": signal.get("signal"),
        "structured_series_rows": len(structured_series),
        "replaced": replaced,
        "retry_pending_compressions": retry_pending_compressions,
    }


LOCAL_AGENT_MAX_QUEUE_LOOKBACK_HOURS = 24 * 365
LOCAL_AGENT_STAGE_PLAN_MAX_SIGNAL_LOOKBACK_HOURS = 96
LOCAL_AGENT_QUEUE_STRATEGIES = {"newest", "oldest", "balanced"}
LOCAL_AGENT_PARSE_FAILURE_RETRY_BUDGET_DEFAULT = 2
LOCAL_AGENT_PARSE_FAILURE_RETRY_LOOKBACK_HOURS_DEFAULT = 24 * 14
LOCAL_AGENT_PREP_DEFERRED_RETRY_COOLDOWN_MINUTES_DEFAULT = 60


def _normalize_queue_strategy(value: object) -> str:
    strategy = str(value or "newest").strip().lower().replace("_", "-")
    if strategy in {"front-to-back", "front_to_back", "oldest-first", "oldest_first"}:
        return "oldest"
    if strategy in {"balanced", "front-back", "front_back"}:
        return "balanced"
    return strategy if strategy in LOCAL_AGENT_QUEUE_STRATEGIES else "newest"


def _parse_failure_retry_budget() -> int:
    try:
        value = int(getattr(settings, "agent_parse_failure_retry_budget", LOCAL_AGENT_PARSE_FAILURE_RETRY_BUDGET_DEFAULT))
    except Exception:
        value = LOCAL_AGENT_PARSE_FAILURE_RETRY_BUDGET_DEFAULT
    return max(0, min(value, 10))


def _parse_failure_retry_lookback_hours() -> int:
    try:
        value = int(
            getattr(
                settings,
                "agent_parse_failure_retry_lookback_hours",
                LOCAL_AGENT_PARSE_FAILURE_RETRY_LOOKBACK_HOURS_DEFAULT,
            )
        )
    except Exception:
        value = LOCAL_AGENT_PARSE_FAILURE_RETRY_LOOKBACK_HOURS_DEFAULT
    return max(1, min(value, LOCAL_AGENT_MAX_QUEUE_LOOKBACK_HOURS))


def _prep_deferred_retry_cooldown_minutes() -> int:
    try:
        value = int(
            getattr(
                settings,
                "agent_prep_deferred_retry_cooldown_minutes",
                LOCAL_AGENT_PREP_DEFERRED_RETRY_COOLDOWN_MINUTES_DEFAULT,
            )
        )
    except Exception:
        value = LOCAL_AGENT_PREP_DEFERRED_RETRY_COOLDOWN_MINUTES_DEFAULT
    return max(5, min(value, 24 * 60))


def _parse_failure_error_filter() -> Any:
    text_expr = func.lower(
        func.coalesce(LocalAgentAction.error, "") + " " + func.coalesce(LocalAgentAction.summary, "")
    )
    return or_(*(text_expr.like(f"%{marker}%") for marker in MODEL_OUTPUT_PARSE_ERROR_MARKERS))


def _parse_failure_retry_exhausted_document_ids(session: Session) -> set[int]:
    budget = _parse_failure_retry_budget()
    if budget <= 0:
        return set()
    cutoff = utcnow() - timedelta(hours=_parse_failure_retry_lookback_hours())
    count_expr = func.count(LocalAgentAction.id)
    rows = (
        session.query(LocalAgentAction.document_id, count_expr.label("parse_failures"))
        .filter(LocalAgentAction.document_id.isnot(None))
        .filter(LocalAgentAction.created_at >= cutoff)
        .filter(LocalAgentAction.action_kind == "signal_extraction_model_error")
        .filter(_parse_failure_error_filter())
        .group_by(LocalAgentAction.document_id)
        .having(count_expr >= budget)
        .limit(10000)
        .all()
    )
    return {int(row[0]) for row in rows if row[0] is not None}


def _stage_failure_retry_exhausted_document_ids(
    session: Session,
    *,
    prompt_version: str | None = None,
) -> set[int]:
    budget = _parse_failure_retry_budget()
    if budget <= 0:
        return set()
    cutoff = utcnow() - timedelta(hours=_parse_failure_retry_lookback_hours())
    rows = (
        session.query(LocalAgentDocumentStageState.document_id)
        .filter(LocalAgentDocumentStageState.document_id.isnot(None))
        .filter(LocalAgentDocumentStageState.failure_count >= budget)
        .filter(LocalAgentDocumentStageState.last_attempt_at.isnot(None))
        .filter(LocalAgentDocumentStageState.last_attempt_at >= cutoff)
    )
    prompt_version_value = str(prompt_version or "").strip()[:64]
    if prompt_version_value:
        rows = rows.filter(
            or_(
                LocalAgentDocumentStageState.prompt_version.is_(None),
                LocalAgentDocumentStageState.prompt_version == prompt_version_value,
            )
        )
    return {int(row[0]) for row in rows.limit(10000).all() if row[0] is not None}


def _retry_pending_read_exhausted_document_ids(session: Session) -> set[int]:
    budget = _parse_failure_retry_budget()
    if budget <= 0:
        return set()
    cutoff = utcnow() - timedelta(hours=_parse_failure_retry_lookback_hours())
    retry_marked_at = func.coalesce(
        LocalAgentDocumentCompression.updated_at,
        LocalAgentDocumentCompression.created_at,
        cutoff,
    )
    read_count = func.count(func.distinct(LocalAgentDocumentRead.id))
    rows = (
        session.query(LocalAgentDocumentCompression.document_id, read_count.label("read_attempts"))
        .join(
            LocalAgentDocumentRead,
            and_(
                LocalAgentDocumentRead.document_id == LocalAgentDocumentCompression.document_id,
                LocalAgentDocumentRead.created_at >= retry_marked_at,
                LocalAgentDocumentRead.created_at >= cutoff,
            ),
        )
        .filter(LocalAgentDocumentCompression.document_id.isnot(None))
        .filter(LocalAgentDocumentCompression.status == TEXT_READY_RETRY_STATUS)
        .group_by(LocalAgentDocumentCompression.document_id)
        .having(read_count >= budget)
        .limit(10000)
        .all()
    )
    return {int(row[0]) for row in rows if row[0] is not None}


def _prep_deferred_cooldown_document_ids(session: Session) -> set[int]:
    budget = _parse_failure_retry_budget()
    if budget <= 0:
        return set()
    cutoff = utcnow() - timedelta(minutes=_prep_deferred_retry_cooldown_minutes())
    count_expr = func.count(LocalAgentAction.id)
    rows = (
        session.query(LocalAgentAction.document_id, count_expr.label("prep_deferrals"))
        .filter(LocalAgentAction.document_id.isnot(None))
        .filter(LocalAgentAction.created_at >= cutoff)
        .filter(LocalAgentAction.action_kind == "document_prep_deferred")
        .group_by(LocalAgentAction.document_id)
        .having(count_expr >= budget)
        .limit(10000)
        .all()
    )
    return {int(row[0]) for row in rows if row[0] is not None}


def _agent_queue_scope(
    session: Session,
    *,
    lookback_hours: int | None = None,
    include_filings: bool = False,
    include_opened: bool = False,
    include_product_snapshots: bool = False,
    prompt_version: str | None = None,
) -> tuple[Any, int, datetime, Any]:
    hours = max(
        1,
        min(
            int(lookback_hours or getattr(settings, "agent_lookback_hours", 48) or 48),
            LOCAL_AGENT_MAX_QUEUE_LOOKBACK_HOURS,
        ),
    )
    cutoff = utcnow() - timedelta(hours=hours)
    terminal_statuses = ["compressed", "attached", "written", "skipped", "noop"]
    if not include_opened:
        # Parse-failure/error compressions are terminal for the fresh-intake rail so
        # one malformed response cannot make the worker retry the same document forever.
        # Explicit backlog/opened scans may still revisit them after the worker/prompt is hardened.
        terminal_statuses.append("error")
    compressed_doc_ids = session.query(LocalAgentDocumentCompression.document_id).filter(
        LocalAgentDocumentCompression.status.in_(terminal_statuses)
    )
    prompt_version_value = str(prompt_version or "").strip()
    if prompt_version_value:
        compressed_doc_ids = compressed_doc_ids.filter(LocalAgentDocumentCompression.prompt_version == prompt_version_value[:64])
    retry_pending_doc_ids = (
        session.query(LocalAgentDocumentCompression.document_id)
        .filter(LocalAgentDocumentCompression.document_id.isnot(None))
        .filter(LocalAgentDocumentCompression.status == TEXT_READY_RETRY_STATUS)
    )
    stage_failure_exhausted_doc_ids = _stage_failure_retry_exhausted_document_ids(
        session,
        prompt_version=prompt_version,
    )
    read_exhausted_doc_ids = _retry_pending_read_exhausted_document_ids(session)
    retry_exhausted_doc_ids = stage_failure_exhausted_doc_ids | read_exhausted_doc_ids
    prep_deferred_cooldown_doc_ids = _prep_deferred_cooldown_document_ids(session)
    queue_blocked_doc_ids = retry_exhausted_doc_ids | prep_deferred_cooldown_doc_ids
    if retry_exhausted_doc_ids:
        retry_pending_doc_ids = retry_pending_doc_ids.filter(
            ~LocalAgentDocumentCompression.document_id.in_(sorted(retry_exhausted_doc_ids))
        )
    if prompt_version_value:
        terminal_for_prompt = aliased(LocalAgentDocumentCompression)
        terminal_for_prompt_doc_ids = (
            session.query(terminal_for_prompt.document_id)
            .filter(terminal_for_prompt.document_id.isnot(None))
            .filter(terminal_for_prompt.status.in_(terminal_statuses))
            .filter(terminal_for_prompt.prompt_version == prompt_version_value[:64])
        )
        retry_pending_doc_ids = retry_pending_doc_ids.filter(
            ~LocalAgentDocumentCompression.document_id.in_(terminal_for_prompt_doc_ids)
        )
    retry_pending_expr = Document.id.in_(retry_pending_doc_ids)
    parse_failure_exhausted_doc_ids = _parse_failure_retry_exhausted_document_ids(session)
    parse_failure_allowed_expr = (
        ~Document.id.in_(sorted(parse_failure_exhausted_doc_ids))
        if parse_failure_exhausted_doc_ids
        else (Document.id != -1)
    )
    queue_blocked_allowed_expr = (
        ~Document.id.in_(sorted(queue_blocked_doc_ids))
        if queue_blocked_doc_ids
        else (Document.id != -1)
    )
    watchlist_rows = (
        session.query(ResearchInstrument.company_id, ResearchInstrument.issuer_id)
        .join(ResearchUniverseRow, ResearchUniverseRow.instrument_id == ResearchInstrument.id)
        .join(ResearchUniverse, ResearchUniverse.id == ResearchUniverseRow.universe_id)
        .filter(ResearchUniverse.status == "active")
        .filter(ResearchUniverseRow.archived_at.is_(None))
        .filter(or_(ResearchInstrument.company_id.isnot(None), ResearchInstrument.issuer_id.isnot(None)))
        .all()
    )
    watchlist_company_ids = {int(row[0]) for row in watchlist_rows if row[0] is not None}
    watchlist_issuer_ids = {int(row[1]) for row in watchlist_rows if row[1] is not None}
    watchlist_earnings_doc_ids: set[int] = set()
    if watchlist_company_ids:
        watchlist_earnings_doc_ids.update(
            int(row[0])
            for row in (
                session.query(CompanyDocument.document_id)
                .filter(CompanyDocument.company_id.in_(sorted(watchlist_company_ids)))
                .filter(CompanyDocument.doc_type.in_(["earnings_release", "earnings_transcript", "transcript", "financial_report", "filing"]))
                .all()
            )
            if row[0] is not None
        )
    if watchlist_issuer_ids:
        watchlist_earnings_doc_ids.update(
            int(row[0])
            for row in (
                session.query(SecuritiesFiling.document_id)
                .filter(SecuritiesFiling.issuer_id.in_(sorted(watchlist_issuer_ids)), SecuritiesFiling.document_id.isnot(None))
                .all()
            )
            if row[0] is not None
        )
    watchlist_earnings_expr = Document.id.in_(sorted(watchlist_earnings_doc_ids)) if watchlist_earnings_doc_ids else (Document.id == -1)
    priority = case(
        (retry_pending_expr, -1),
        (Document.kind.ilike("%transcript%"), 0),
        (Document.kind.in_(["earnings_transcript", "transcript"]), 0),
        (watchlist_earnings_expr, 0),
        (Document.saved.is_(True), 1),
        else_=2,
    )
    date_expr = func.coalesce(Document.published_at, Document.ingested_at)
    rows = (
        session.query(Document)
        .join(Source, Source.id == Document.source_id)
        .filter(or_(date_expr >= cutoff, retry_pending_expr))
        .filter(~Document.kind.in_(["agent_digest", "agent_context_compression"]))
        .filter(agent_intake_visible_filter(session))
        .filter(or_(retry_pending_expr, ~Document.id.in_(compressed_doc_ids)))
        .filter(parse_failure_allowed_expr)
        .filter(queue_blocked_allowed_expr)
    )
    if include_opened:
        rows = rows.filter(or_(retry_pending_expr, Document.triage_state.in_(["NEW", "OPENED"])))
    else:
        rows = rows.filter(or_(retry_pending_expr, Document.triage_state == "NEW"))
    if not include_product_snapshots:
        rows = rows.filter(Document.kind != "product_snapshot")
    if not include_filings:
        periodic_filing_docs = session.query(SecuritiesFiling.document_id).filter(
            SecuritiesFiling.document_id.isnot(None),
            func.upper(func.coalesce(SecuritiesFiling.filing_type, "")).in_(["10-Q", "10-Q/A", "10-K", "10-K/A"]),
        )
        filing_source = or_(
            func.coalesce(Source.name, "").ilike("%sec%"),
            func.coalesce(Source.name, "").ilike("%filing%"),
            func.coalesce(Source.name, "").ilike("%filings%"),
            func.coalesce(Source.category, "").ilike("%filing%"),
        )
        filing_title = or_(
            func.coalesce(Document.title, "").ilike("10-Q%"),
            func.coalesce(Document.title, "").ilike("10-K%"),
            func.coalesce(Document.title, "").ilike("FORM 10-Q%"),
            func.coalesce(Document.title, "").ilike("FORM 10-K%"),
            func.coalesce(Document.title, "").ilike("SCHEDULE %"),
            func.coalesce(Document.title, "").ilike("Form SCHEDULE %"),
            func.coalesce(Document.title, "").ilike("% - 8-K%"),
            func.coalesce(Document.title, "").ilike("% - 10-K%"),
            func.coalesce(Document.title, "").ilike("% - 10-Q%"),
            func.coalesce(Document.title, "").ilike("%: 8-K%"),
            func.coalesce(Document.title, "").ilike("%: 10-K%"),
            func.coalesce(Document.title, "").ilike("%: 10-Q%"),
        )
        rows = rows.filter(
            or_(
                watchlist_earnings_expr,
                and_(Document.kind != "sec_filing", ~Document.id.in_(periodic_filing_docs), ~filing_source, ~filing_title),
            )
        )
    return rows, hours, cutoff, priority


def _agent_queue_order(priority: Any, *, queue_strategy: object = "newest") -> list[Any]:
    strategy = _normalize_queue_strategy(queue_strategy)
    recency_expr = func.coalesce(Document.published_at, Document.ingested_at)
    if strategy == "oldest":
        return [
            priority.asc(),
            Document.importance.desc(),
            recency_expr.asc(),
            Document.id.asc(),
        ]
    return [
        priority.asc(),
        Document.importance.desc(),
        recency_expr.desc(),
        Document.id.desc(),
    ]


def _document_queue_preview(row: Document) -> dict[str, Any]:
    source = row.source
    return {
        "id": int(row.id),
        "kind": row.kind,
        "source_id": int(row.source_id),
        "source_name": source.name if source else None,
        "title": row.title,
        "url": row.url or row.canonical_url,
        "published_at": _dt(row.published_at),
        "ingested_at": _dt(row.ingested_at),
        "triage_state": row.triage_state,
        "importance": int(row.importance or 0),
        "language": row.language,
    }


_BATCH_STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "amid",
    "among",
    "and",
    "announces",
    "announcement",
    "are",
    "around",
    "before",
    "being",
    "between",
    "but",
    "company",
    "could",
    "during",
    "earnings",
    "from",
    "has",
    "have",
    "into",
    "its",
    "market",
    "more",
    "new",
    "not",
    "out",
    "over",
    "press",
    "release",
    "report",
    "reports",
    "said",
    "says",
    "shares",
    "than",
    "that",
    "the",
    "this",
    "update",
    "was",
    "were",
    "with",
}
_BATCH_STRONG_ENTITY_KINDS = {"company", "ticker"}
_BATCH_PLAN_TEXT_PREVIEW_CHARS = 800


def _queue_estimated_tokens(text: object) -> int:
    body = str(text or "")
    words = len(re.findall(r"\S+", body))
    return max(1, int(max(len(body) / 3.5, words * 1.3)))


def _queue_estimated_tokens_from_length(text_chars: object, text_preview: object) -> int:
    try:
        char_count = max(0, int(text_chars or 0))
    except Exception:
        char_count = len(str(text_preview or ""))
    preview_words = len(re.findall(r"\S+", str(text_preview or "")))
    return max(1, int(max(char_count / 3.5, preview_words * 1.3)))


def _queue_date_bucket(row: Document) -> str:
    value = row.published_at or row.ingested_at
    return value.date().isoformat() if value else "undated"


def _queue_keyword_set(row: Document) -> set[str]:
    raw = " ".join(str(value or "") for value in (row.title, row.normalized_text[:800] if row.normalized_text else ""))
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", raw)
        if token.lower() not in _BATCH_STOPWORDS and len(token) >= 3
    }
    return set(sorted(tokens)[:24])


def _queue_entity_names(row: Document) -> list[str]:
    out: list[str] = []
    for link in row.entity_links or []:
        entity = link.entity if isinstance(link, DocumentEntity) else None
        name = _clean(entity.canonical_name if isinstance(entity, Entity) else "")
        if name:
            out.append(name)
    return sorted(set(out), key=lambda value: value.lower())


def _queue_entity_anchor(row: Document) -> str:
    candidates: list[tuple[int, str, str]] = []
    kind_rank = {"company": 0, "ticker": 1}
    title_upper = str(row.title or "").upper()
    text_upper = " ".join(str(value or "") for value in (row.title, row.normalized_text[:800] if row.normalized_text else "")).upper()
    for link in row.entity_links or []:
        entity = link.entity if isinstance(link, DocumentEntity) else None
        name = _clean(entity.canonical_name if isinstance(entity, Entity) else "")
        kind = _clean(entity.kind if isinstance(entity, Entity) else "").lower()
        if not name or kind not in _BATCH_STRONG_ENTITY_KINDS:
            continue
        name_key = name.lower()
        if name_key in _BATCH_STOPWORDS:
            continue
        if kind == "company" and name.upper() not in title_upper and f"${name.upper()}" not in text_upper:
            continue
        if kind == "ticker" and len(name_key) <= 2 and f"${name.upper()}" not in text_upper:
            continue
        if kind == "ticker" and (not name.isdigit()) and name.upper() not in title_upper and f"${name.upper()}" not in text_upper:
            continue
        candidates.append((kind_rank.get(kind, 99), name.lower(), name))
    if not candidates:
        return ""
    return sorted(candidates)[0][2]


def _queue_tickers(row: Document) -> list[str]:
    text = " ".join(str(value or "") for value in (row.title, row.normalized_text[:800] if row.normalized_text else ""))
    tickers = {token.upper() for token in re.findall(r"(?<![A-Za-z0-9])\$([A-Z][A-Z0-9.\-]{0,7})(?![A-Za-z0-9])", text)}
    return sorted(tickers)


def _queue_title_signature(row: Document) -> str:
    tokens = sorted(_queue_keyword_set(row))
    if len(tokens) < 3:
        return ""
    return "-".join(tokens[:8])


def _queue_cluster_anchor(row: Document) -> tuple[str, str]:
    entity_anchor = _queue_entity_anchor(row)
    if entity_anchor:
        return "entity", f"{entity_anchor.lower()}:{_queue_date_bucket(row)}"
    tickers = _queue_tickers(row)
    if tickers:
        return "ticker", f"{tickers[0]}:{_queue_date_bucket(row)}"
    title_sig = _queue_title_signature(row)
    if title_sig:
        return "title", title_sig
    return "single", f"document:{int(row.id)}"


def _projected_queue_keyword_set(title: object, text_preview: object) -> set[str]:
    raw = " ".join(str(value or "") for value in (title, text_preview))
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", raw)
        if token.lower() not in _BATCH_STOPWORDS and len(token) >= 3
    }
    return set(sorted(tokens)[:24])


def _projected_queue_entity_names(entities: list[dict[str, str]]) -> list[str]:
    names = [_clean(entity.get("name")) for entity in entities]
    return sorted({name for name in names if name}, key=lambda value: value.lower())


def _projected_queue_entity_anchor(title: object, text_preview: object, entities: list[dict[str, str]]) -> str:
    candidates: list[tuple[int, str, str]] = []
    kind_rank = {"company": 0, "ticker": 1}
    title_upper = str(title or "").upper()
    text_upper = " ".join(str(value or "") for value in (title, text_preview)).upper()
    for entity in entities:
        name = _clean(entity.get("name"))
        kind = _clean(entity.get("kind")).lower()
        if not name or kind not in _BATCH_STRONG_ENTITY_KINDS:
            continue
        name_key = name.lower()
        if name_key in _BATCH_STOPWORDS:
            continue
        if kind == "company" and name.upper() not in title_upper and f"${name.upper()}" not in text_upper:
            continue
        if kind == "ticker" and len(name_key) <= 2 and f"${name.upper()}" not in text_upper:
            continue
        if kind == "ticker" and (not name.isdigit()) and name.upper() not in title_upper and f"${name.upper()}" not in text_upper:
            continue
        candidates.append((kind_rank.get(kind, 99), name.lower(), name))
    if not candidates:
        return ""
    return sorted(candidates)[0][2]


def _projected_queue_tickers(title: object, text_preview: object) -> list[str]:
    text = " ".join(str(value or "") for value in (title, text_preview))
    tickers = {token.upper() for token in re.findall(r"(?<![A-Za-z0-9])\$([A-Z][A-Z0-9.\-]{0,7})(?![A-Za-z0-9])", text)}
    return sorted(tickers)


def _projected_queue_title_signature(title: object, text_preview: object) -> str:
    tokens = sorted(_projected_queue_keyword_set(title, text_preview))
    if len(tokens) < 3:
        return ""
    return "-".join(tokens[:8])


def _projected_queue_cluster_anchor(row: dict[str, Any], entities: list[dict[str, str]]) -> tuple[str, str]:
    entity_anchor = _projected_queue_entity_anchor(row.get("title"), row.get("text_preview"), entities)
    if entity_anchor:
        return "entity", f"{entity_anchor.lower()}:{row.get('date_bucket') or 'undated'}"
    tickers = _projected_queue_tickers(row.get("title"), row.get("text_preview"))
    if tickers:
        return "ticker", f"{tickers[0]}:{row.get('date_bucket') or 'undated'}"
    title_sig = _projected_queue_title_signature(row.get("title"), row.get("text_preview"))
    if title_sig:
        return "title", title_sig
    return "single", f"document:{int(row['id'])}"


def _batch_plan_document(row: Document, *, rank: int) -> dict[str, Any]:
    preview = _document_queue_preview(row)
    text_preview = str(row.translation_en or row.normalized_text or row.raw_text or "")
    source = row.source if isinstance(row.source, Source) else None
    anchor_kind, anchor_value = _queue_cluster_anchor(row)
    keywords = sorted(_queue_keyword_set(row))[:12]
    preview.update(
        {
            "rank": int(rank),
            "estimated_tokens": _queue_estimated_tokens(text_preview),
            "text_chars": len(text_preview),
            "cluster_anchor": {"kind": anchor_kind, "value": anchor_value},
            "entity_names": _queue_entity_names(row),
            "tickers": _queue_tickers(row),
            "keywords": keywords,
            "source_category": source.category if source else None,
        }
    )
    return preview


def _batch_plan_document_from_projection(
    row: dict[str, Any],
    *,
    rank: int,
    entities: list[dict[str, str]],
) -> dict[str, Any]:
    text_preview = str(row.get("text_preview") or "")
    anchor_kind, anchor_value = _projected_queue_cluster_anchor(row, entities)
    keywords = sorted(_projected_queue_keyword_set(row.get("title"), text_preview))[:12]
    return {
        "id": int(row["id"]),
        "kind": row.get("kind"),
        "source_id": int(row["source_id"]),
        "source_name": row.get("source_name"),
        "title": row.get("title"),
        "url": row.get("url") or row.get("canonical_url"),
        "published_at": _dt(row.get("published_at")),
        "ingested_at": _dt(row.get("ingested_at")),
        "triage_state": row.get("triage_state"),
        "importance": int(row.get("importance") or 0),
        "language": row.get("language"),
        "rank": int(rank),
        "estimated_tokens": _queue_estimated_tokens_from_length(row.get("text_chars"), text_preview),
        "text_chars": int(row.get("text_chars") or len(text_preview)),
        "cluster_anchor": {"kind": anchor_kind, "value": anchor_value},
        "entity_names": _projected_queue_entity_names(entities),
        "tickers": _projected_queue_tickers(row.get("title"), text_preview),
        "keywords": keywords,
        "source_category": row.get("source_category"),
    }


def _batch_plan_entity_map(session: Session, document_ids: list[int]) -> dict[int, list[dict[str, str]]]:
    if not document_ids:
        return {}
    rows = (
        session.query(DocumentEntity.document_id, Entity.canonical_name, Entity.kind)
        .join(Entity, Entity.id == DocumentEntity.entity_id)
        .filter(DocumentEntity.document_id.in_(document_ids))
        .order_by(DocumentEntity.document_id.asc(), Entity.canonical_name.asc())
        .all()
    )
    by_doc: dict[int, list[dict[str, str]]] = {}
    for doc_id, name, kind in rows:
        clean_name = _clean(name)
        if not clean_name:
            continue
        by_doc.setdefault(int(doc_id), []).append({"name": clean_name, "kind": _clean(kind).lower()})
    return by_doc


def _batch_plan_projected_candidates(
    rows: Any,
    priority: Any,
    *,
    limit: int,
    queue_strategy: object = "newest",
    balance_focus_limit: int | None = None,
) -> list[dict[str, Any]]:
    text_expr = func.coalesce(Document.translation_en, Document.normalized_text, Document.raw_text, "")
    preview_expr = func.substr(text_expr, 1, _BATCH_PLAN_TEXT_PREVIEW_CHARS)
    date_expr = func.coalesce(Document.published_at, Document.ingested_at)
    retry_pending_expr = (
        rows.session.query(LocalAgentDocumentCompression.id)
        .filter(
            LocalAgentDocumentCompression.document_id == Document.id,
            LocalAgentDocumentCompression.status == TEXT_READY_RETRY_STATUS,
        )
        .exists()
    )

    def fetch(strategy: str, row_limit: int) -> list[Any]:
        if row_limit <= 0:
            return []
        return (
            rows.with_entities(
                Document.id.label("id"),
                Document.kind.label("kind"),
                Document.source_id.label("source_id"),
                Source.name.label("source_name"),
                Source.category.label("source_category"),
                Document.title.label("title"),
                Document.url.label("url"),
                Document.canonical_url.label("canonical_url"),
                Document.published_at.label("published_at"),
                Document.ingested_at.label("ingested_at"),
                Document.triage_state.label("triage_state"),
                Document.importance.label("importance"),
                Document.language.label("language"),
                func.length(text_expr).label("text_chars"),
                preview_expr.label("text_preview"),
                func.date(date_expr).label("date_bucket"),
                retry_pending_expr.label("retry_pending"),
            )
            .order_by(*_agent_queue_order(priority, queue_strategy=strategy))
            .limit(row_limit)
            .all()
        )

    strategy = _normalize_queue_strategy(queue_strategy)
    if strategy == "balanced" and limit > 1:
        focus_limit = max(2, min(int(balance_focus_limit or limit), int(limit)))
        head_limit = max(1, (focus_limit + 1) // 2)
        tail_limit = max(0, focus_limit - head_limit)
        projected = []
        seen_ids: set[int] = set()
        for row in [*fetch("newest", head_limit), *fetch("oldest", tail_limit)]:
            row_id = int(row.id)
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            projected.append(row)
        if len(projected) < limit:
            for row in fetch("newest", int(limit)):
                row_id = int(row.id)
                if row_id in seen_ids:
                    continue
                seen_ids.add(row_id)
                projected.append(row)
                if len(projected) >= limit:
                    break
    else:
        projected = fetch(strategy, int(limit))

    out: list[dict[str, Any]] = []
    for row in projected:
        out.append(
            {
                "id": int(row.id),
                "kind": row.kind,
                "source_id": int(row.source_id),
                "source_name": row.source_name,
                "source_category": row.source_category,
                "title": row.title,
                "url": row.url,
                "canonical_url": row.canonical_url,
                "published_at": row.published_at,
                "ingested_at": row.ingested_at,
                "triage_state": row.triage_state,
                "importance": row.importance,
                "language": row.language,
                "text_chars": int(row.text_chars or 0),
                "text_preview": str(row.text_preview or ""),
                "date_bucket": str(row.date_bucket or "undated"),
                "retry_pending": bool(row.retry_pending),
            }
        )
    return out


def _document_text_snippets(session: Session, document_ids: list[int], *, max_chars: int) -> dict[int, str]:
    if not document_ids:
        return {}
    limit = max(1, int(max_chars or 1))
    text_expr = func.coalesce(Document.translation_en, Document.normalized_text, Document.raw_text, "")
    rows = (
        session.query(Document.id, func.substr(text_expr, 1, limit).label("text_snippet"))
        .filter(Document.id.in_(document_ids))
        .all()
    )
    return {int(doc_id): str(text_snippet or "") for doc_id, text_snippet in rows}


def _queue_read_preserves_full_filing_review(doc: Document) -> bool:
    source = doc.source if isinstance(doc.source, Source) else None
    kind = str(doc.kind or "").strip().lower()
    title = str(doc.title or "").upper()
    source_text = " ".join(str(value or "") for value in (source.name if source else "", source.category if source else "")).lower()
    return (
        kind == "sec_filing"
        or "filing" in source_text
        or title.startswith(("10-Q", "10-K", "FORM 10-Q", "FORM 10-K"))
        or " - 10-Q" in title
        or " - 10-K" in title
        or ": 10-Q" in title
        or ": 10-K" in title
    )


def _planned_batch(batch_id: str, rows: list[dict[str, Any]], *, mode: str, reason: str, max_input_tokens: int) -> dict[str, Any]:
    estimated = sum(int(row.get("estimated_tokens") or 1) for row in rows)
    return {
        "batch_id": batch_id,
        "mode": mode,
        "reason": reason,
        "document_ids": [int(row["id"]) for row in rows],
        "estimated_input_tokens": estimated,
        "max_input_tokens": int(max_input_tokens),
        "documents": rows,
    }


def _batch_plan_document_ids(plan: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for batch in _json_list(plan.get("batches")):
        if not isinstance(batch, dict):
            continue
        for value in _json_list(batch.get("document_ids")):
            try:
                doc_id = int(value)
            except Exception:
                continue
            if doc_id not in out:
                out.append(doc_id)
    return out


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _batch_plan_oldest_age_hours(plan: dict[str, Any], *, now: datetime) -> float | None:
    oldest: datetime | None = None
    for batch in _json_list(plan.get("batches")):
        if not isinstance(batch, dict):
            continue
        for doc in _json_list(batch.get("documents")):
            if not isinstance(doc, dict):
                continue
            value = _coerce_datetime(doc.get("published_at") or doc.get("ingested_at"))
            if value is None:
                continue
            if value.tzinfo is not None and now.tzinfo is None:
                value = value.replace(tzinfo=None)
            if value.tzinfo is None and now.tzinfo is not None:
                value = value.replace(tzinfo=now.tzinfo)
            if oldest is None or value < oldest:
                oldest = value
    if oldest is None:
        return None
    return max(0.0, (now - oldest).total_seconds() / 3600.0)


def _batch_plan_window_fill_ratio(plan: dict[str, Any], *, now: datetime) -> float | None:
    try:
        hours = max(1, int(plan.get("lookback_hours") or 1))
    except Exception:
        return None
    oldest_age = _batch_plan_oldest_age_hours(plan, now=now)
    if oldest_age is None:
        return None
    return max(0.0, min(1.0, float(oldest_age) / float(hours)))


def _batch_plan_key(row: dict[str, Any]) -> tuple[str, str]:
    anchor = row.get("cluster_anchor") if isinstance(row.get("cluster_anchor"), dict) else {}
    return (str(anchor.get("kind") or "single"), str(anchor.get("value") or f"document:{row['id']}"))


def get_document_batch_plan(
    session: Session,
    *,
    limit: int = 4,
    lookback_hours: int | None = None,
    include_filings: bool = False,
    include_opened: bool = False,
    include_product_snapshots: bool = False,
    defer_pending_media: bool = False,
    max_input_tokens: int = 32_000,
    max_documents_per_batch: int = 6,
    queue_strategy: str = "newest",
    prompt_version: str | None = None,
) -> dict[str, Any]:
    resolved_limit = _limit(limit, default=4, high=50)
    resolved_budget = max(2_000, min(int(max_input_tokens or 32_000), 64_000))
    resolved_max_docs = max(1, min(int(max_documents_per_batch or 6), min(12, resolved_limit)))
    strategy = _normalize_queue_strategy(queue_strategy)
    rows, hours, cutoff, priority = _agent_queue_scope(
        session,
        lookback_hours=lookback_hours,
        include_filings=include_filings,
        include_opened=include_opened,
        include_product_snapshots=include_product_snapshots,
        prompt_version=prompt_version,
    )
    candidate_limit = max(resolved_limit, min(resolved_limit * 4, 100))
    query_limit = min(250, candidate_limit * 3) if defer_pending_media else candidate_limit
    candidates = _batch_plan_projected_candidates(rows, priority, limit=query_limit, queue_strategy=strategy, balance_focus_limit=resolved_limit)
    deferred_pending_media = 0
    deferred_pending_media_ids: list[int] = []
    if defer_pending_media:
        candidates, deferred_pending_media, deferred_pending_media_ids = _defer_media_pending_document_plan_rows(
            session,
            candidates,
            limit=candidate_limit,
        )
    entity_map = _batch_plan_entity_map(session, [int(row["id"]) for row in candidates])
    docs = [
        _batch_plan_document_from_projection(
            row,
            rank=rank,
            entities=entity_map.get(int(row["id"]), []),
        )
        for rank, row in enumerate(candidates)
    ]
    docs_by_id = {int(row["id"]): row for row in docs}
    retry_pending_plan_doc_ids = {int(row["id"]) for row in candidates if row.get("retry_pending")}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in docs:
        groups.setdefault(_batch_plan_key(row), []).append(row)

    used: set[int] = set()
    batches: list[dict[str, Any]] = []
    oldest_balanced_row: dict[str, Any] | None = None
    if strategy == "balanced" and resolved_limit > 1 and len(docs) > resolved_limit:
        dated_rows: list[tuple[datetime, dict[str, Any]]] = []
        for row in docs:
            value = _coerce_datetime(row.get("published_at") or row.get("ingested_at"))
            if value is not None:
                if value.tzinfo is not None:
                    value = value.replace(tzinfo=None)
                dated_rows.append((value, row))
        if dated_rows:
            oldest_balanced_row = min(dated_rows, key=lambda item: item[0])[1]
    oldest_balanced_doc_id = int(oldest_balanced_row["id"]) if oldest_balanced_row is not None else None

    def add_batch(rows_to_add: list[dict[str, Any]], *, mode: str, reason: str) -> None:
        if not rows_to_add:
            return
        batch_rows: list[dict[str, Any]] = []
        tokens = 0
        for row in rows_to_add:
            doc_id = int(row["id"])
            if doc_id in used:
                continue
            row_tokens = int(row.get("estimated_tokens") or 1)
            if batch_rows and (tokens + row_tokens > resolved_budget or len(batch_rows) >= resolved_max_docs):
                batch_id = f"batch-{len(batches) + 1}"
                planned_mode = mode if len(batch_rows) > 1 else "single"
                planned_reason = reason if len(batch_rows) > 1 else "no_conservative_cluster"
                batches.append(_planned_batch(batch_id, batch_rows, mode=planned_mode, reason=planned_reason, max_input_tokens=resolved_budget))
                for item in batch_rows:
                    used.add(int(item["id"]))
                batch_rows = []
                tokens = 0
            batch_rows.append(row)
            tokens += row_tokens
            if len(used) + len(batch_rows) >= resolved_limit:
                break
        if batch_rows:
            batch_id = f"batch-{len(batches) + 1}"
            planned_mode = mode if len(batch_rows) > 1 else "single"
            planned_reason = reason if len(batch_rows) > 1 else "no_conservative_cluster"
            batches.append(_planned_batch(batch_id, batch_rows, mode=planned_mode, reason=planned_reason, max_input_tokens=resolved_budget))
            for item in batch_rows:
                used.add(int(item["id"]))

    conservative_group_keys = {
        key
        for key, group in groups.items()
        if key[0] in {"entity", "ticker", "title"} and len(group) >= 2
    }
    head_single_quota = min(2, max(0, resolved_limit - 2)) if conservative_group_keys else resolved_limit
    deferred_single_doc_ids: list[int] = []
    head_singles_added = 0

    def add_single(row: dict[str, Any]) -> None:
        add_batch([docs_by_id[int(row["id"])]], mode="single", reason="no_conservative_cluster")

    for row in docs:
        if len(used) >= resolved_limit:
            break
        doc_id = int(row["id"])
        if oldest_balanced_doc_id is not None and doc_id != oldest_balanced_doc_id and len(used) >= resolved_limit - 1:
            break
        if doc_id in retry_pending_plan_doc_ids and doc_id not in used:
            add_single(row)

    if oldest_balanced_row is not None and oldest_balanced_doc_id not in used and len(used) < resolved_limit:
        add_single(oldest_balanced_row)

    for row in docs:
        if len(used) >= resolved_limit:
            break
        doc_id = int(row["id"])
        if doc_id in used:
            continue
        key = _batch_plan_key(row)
        group = [item for item in groups.get(key, []) if int(item["id"]) not in used]
        if key in conservative_group_keys and len(group) >= 2:
            add_batch(group, mode="packed", reason=f"shared_{key[0]}")
        else:
            if head_singles_added < head_single_quota:
                add_single(row)
                head_singles_added += 1
            else:
                deferred_single_doc_ids.append(doc_id)

    for doc_id in deferred_single_doc_ids:
        if len(used) >= resolved_limit:
            break
        if doc_id in used:
            continue
        add_single(docs_by_id[doc_id])

    return {
        "lookback_hours": hours,
        "cutoff_at": _dt(cutoff),
        "include_opened": bool(include_opened),
        "include_filings": bool(include_filings),
        "include_product_snapshots": bool(include_product_snapshots),
        "queue_strategy": strategy,
        "prompt_version": str(prompt_version or "").strip() or None,
        "limit": resolved_limit,
        "candidate_limit": candidate_limit,
        "documents_considered": len(docs),
        "documents_planned": len(used),
        "defer_pending_media": bool(defer_pending_media),
        "documents_deferred_pending_media": deferred_pending_media,
        "documents_deferred_pending_media_ids": deferred_pending_media_ids,
        "packing_strategy": {
            "mode": "balanced_head_single_lookahead",
            "queue_strategy": strategy,
            "head_single_quota": head_single_quota,
            "conservative_groups": len(conservative_group_keys),
        },
        "max_input_tokens": resolved_budget,
        "max_documents_per_batch": resolved_max_docs,
        "batches": batches,
    }


def get_next_documents(
    session: Session,
    *,
    limit: int = 4,
    lookback_hours: int | None = None,
    include_filings: bool = False,
    include_opened: bool = False,
    include_product_snapshots: bool = False,
    defer_pending_media: bool = False,
    queue_strategy: str = "newest",
    prompt_version: str | None = None,
) -> list[dict[str, Any]]:
    resolved_limit = _limit(limit, default=4, high=50)
    strategy = _normalize_queue_strategy(queue_strategy)
    rows, _hours, _cutoff, priority = _agent_queue_scope(
        session,
        lookback_hours=lookback_hours,
        include_filings=include_filings,
        include_opened=include_opened,
        include_product_snapshots=include_product_snapshots,
        prompt_version=prompt_version,
    )
    base_query = (
        rows.options(
            load_only(
                Document.id,
                Document.kind,
                Document.source_id,
                Document.title,
                Document.author,
                Document.url,
                Document.canonical_url,
                Document.published_at,
                Document.ingested_at,
                Document.triage_state,
                Document.importance,
                Document.language,
                Document.extra,
            ),
            joinedload(Document.source).load_only(Source.id, Source.name, Source.category),
            joinedload(Document.attachments).load_only(
                Attachment.id,
                Attachment.document_id,
                Attachment.kind,
                Attachment.content_type,
                Attachment.object_path,
                Attachment.original_url,
                Attachment.sha256,
            ),
        )
    )
    candidate_limit = min(250, resolved_limit * 3) if defer_pending_media else resolved_limit
    if strategy == "balanced" and candidate_limit > 1:
        head_limit = max(1, (candidate_limit + 1) // 2)
        tail_limit = max(0, candidate_limit - head_limit)
        docs = []
        seen_ids: set[int] = set()
        for row in [
            *base_query.order_by(*_agent_queue_order(priority, queue_strategy="newest")).limit(head_limit).all(),
            *base_query.order_by(*_agent_queue_order(priority, queue_strategy="oldest")).limit(tail_limit).all(),
        ]:
            row_id = int(row.id)
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
            docs.append(row)
        if len(docs) < candidate_limit:
            for row in base_query.order_by(*_agent_queue_order(priority, queue_strategy="newest")).limit(candidate_limit).all():
                row_id = int(row.id)
                if row_id in seen_ids:
                    continue
                seen_ids.add(row_id)
                docs.append(row)
                if len(docs) >= candidate_limit:
                    break
    else:
        docs = (
            base_query
            .order_by(*_agent_queue_order(priority, queue_strategy=strategy))
            .limit(candidate_limit)
            .all()
        )
    if defer_pending_media:
        docs, _deferred_pending_media = _defer_media_pending_documents(session, docs, limit=resolved_limit)
    else:
        docs = docs[:resolved_limit]
    text_snippets = _document_text_snippets(session, [int(row.id) for row in docs], max_chars=1600)
    return [
        document_payload(
            row,
            max_text_chars=1600,
            stored_text_override=(
                None if _queue_read_preserves_full_filing_review(row) else text_snippets.get(int(row.id), "")
            ),
        )
        for row in docs
    ]


def _adaptive_stage_signal_batch_plan(
    session: Session,
    *,
    signal_quota: int,
    lookback_hours: int | None,
    queue_strategy: str,
    now: datetime,
    prompt_version: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    strategy = _normalize_queue_strategy(queue_strategy)
    try:
        requested_hours = max(
            1,
            min(
                int(lookback_hours or getattr(settings, "agent_lookback_hours", 48) or 48),
                LOCAL_AGENT_MAX_QUEUE_LOOKBACK_HOURS,
            ),
        )
    except Exception:
        requested_hours = 48
    max_hours = min(LOCAL_AGENT_MAX_QUEUE_LOOKBACK_HOURS, LOCAL_AGENT_STAGE_PLAN_MAX_SIGNAL_LOOKBACK_HOURS)
    planning_hours = min(requested_hours, max_hours)
    plan = get_document_batch_plan(
        session,
        limit=signal_quota,
        lookback_hours=planning_hours,
        include_filings=False,
        include_opened=False,
        defer_pending_media=True,
        max_input_tokens=32_000,
        max_documents_per_batch=min(6, signal_quota),
        queue_strategy=strategy,
        prompt_version=prompt_version,
    )
    try:
        initial_hours = max(1, int(plan.get("lookback_hours") or lookback_hours or getattr(settings, "agent_lookback_hours", 48) or 48))
    except Exception:
        initial_hours = 48
    expansion: dict[str, Any] = {
        "enabled": strategy in {"balanced", "oldest"},
        "strategy": strategy,
        "requested_lookback_hours": requested_hours,
        "planning_lookback_hours": planning_hours,
        "planning_lookback_capped": planning_hours < requested_hours,
        "max_effective_lookback_hours": max_hours,
        "initial_lookback_hours": initial_hours,
        "effective_lookback_hours": initial_hours,
        "expanded": False,
        "attempts": [
            {
                "lookback_hours": initial_hours,
                "documents_planned": int(plan.get("documents_planned") or 0),
                "window_fill_ratio": _batch_plan_window_fill_ratio(plan, now=now),
            }
        ],
        "reason": "not_evaluated",
    }
    if strategy not in {"balanced", "oldest"}:
        expansion["reason"] = "queue_strategy_not_adaptive"
        return plan, expansion
    if signal_quota <= 0 or int(plan.get("documents_planned") or 0) < signal_quota:
        expansion["reason"] = "window_not_full"
        return plan, expansion

    current_hours = initial_hours
    fill_threshold = 0.85
    while current_hours < max_hours:
        fill_ratio = _batch_plan_window_fill_ratio(plan, now=now)
        if fill_ratio is None or fill_ratio < fill_threshold:
            expansion["reason"] = "oldest_tail_not_near_cutoff"
            break
        next_hours = min(max_hours, max(current_hours + 1, current_hours * 2))
        next_plan = get_document_batch_plan(
            session,
            limit=signal_quota,
            lookback_hours=next_hours,
            include_filings=False,
            include_opened=False,
            defer_pending_media=True,
            max_input_tokens=32_000,
            max_documents_per_batch=min(6, signal_quota),
            queue_strategy=strategy,
            prompt_version=prompt_version,
        )
        next_planned = int(next_plan.get("documents_planned") or 0)
        next_fill_ratio = _batch_plan_window_fill_ratio(next_plan, now=now)
        expansion["attempts"].append(
            {
                "lookback_hours": int(next_plan.get("lookback_hours") or next_hours),
                "documents_planned": next_planned,
                "window_fill_ratio": next_fill_ratio,
            }
        )
        plan = next_plan
        current_hours = int(next_plan.get("lookback_hours") or next_hours)
        expansion["expanded"] = True
        expansion["effective_lookback_hours"] = current_hours
        if next_planned < signal_quota:
            expansion["reason"] = "expanded_until_window_not_full"
            break
        if current_hours >= max_hours:
            expansion["reason"] = "max_lookback_reached"
            break
    else:
        expansion["reason"] = "max_lookback_reached"

    if expansion.get("reason") == "not_evaluated":
        expansion["reason"] = "expanded"
    return plan, expansion


def local_agent_stage_plan(
    session: Session,
    *,
    media_limit: int = 2,
    signal_limit: int = 4,
    durable_limit: int = 2,
    lookback_hours: int | None = None,
    queue_strategy: str = "newest",
    prompt_version: str | None = None,
    document_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Read-only staged work plan for balancing prep, signal extraction, and durable review."""
    media_quota = max(0, min(int(media_limit or 0), 50))
    signal_quota = max(1, min(int(signal_limit or 4), 50))
    durable_quota = max(0, min(int(durable_limit or 0), 12))
    strategy = _normalize_queue_strategy(queue_strategy)
    prompt_version_value = str(prompt_version or "").strip()
    now = utcnow()
    requested_document_ids = _int_list(document_ids)[:50]
    if requested_document_ids:
        active_document_ids = {
            document_id
            for document_id in requested_document_ids
            if _agent_can_access_document(session, document_id)
        }
    else:
        active_document_ids = _active_window_document_ids(
            session,
            lookback_hours=lookback_hours,
            prompt_version=prompt_version,
            include_filings=False,
        )
    media_status_rows = (
        session.query(Job)
        .with_entities(Job.status, func.count(Job.id))
        .filter(Job.kind == MEDIA_TRANSCRIPTION_JOB_KIND)
        .filter(Job.status.in_(["pending", "running", "error"]))
        .group_by(Job.status)
        .all()
    )
    media_status_counts = {str(status or "unknown"): int(count or 0) for status, count in media_status_rows}
    active_media_counts = _media_stage_job_counts(session, active_document_ids=active_document_ids)
    actionable_media_jobs = int(active_media_counts.get("ocr_pending_jobs") or 0) + int(active_media_counts.get("vlm_pending_jobs") or 0)
    running_media = int(active_media_counts.get("running_jobs") or 0)
    error_media = (
        int(active_media_counts.get("retryable_error_jobs") or 0)
        + int(active_media_counts.get("retry_delayed_error_jobs") or 0)
        + int(active_media_counts.get("exhausted_error_jobs") or 0)
        if requested_document_ids
        else int(media_status_counts.get("error") or 0)
    )
    retry_signal_query = (
        session.query(LocalAgentDocumentCompression)
        .with_entities(LocalAgentDocumentCompression.document_id)
        .filter(LocalAgentDocumentCompression.document_id.isnot(None))
        .filter(LocalAgentDocumentCompression.status == TEXT_READY_RETRY_STATUS)
    )
    stage_failure_exhausted_doc_ids = _stage_failure_retry_exhausted_document_ids(
        session,
        prompt_version=prompt_version,
    )
    read_exhausted_doc_ids = _retry_pending_read_exhausted_document_ids(session)
    retry_exhausted_doc_ids = stage_failure_exhausted_doc_ids | read_exhausted_doc_ids
    prep_deferred_cooldown_doc_ids = _prep_deferred_cooldown_document_ids(session)
    queue_blocked_doc_ids = retry_exhausted_doc_ids | prep_deferred_cooldown_doc_ids
    if queue_blocked_doc_ids:
        retry_signal_query = retry_signal_query.filter(
            ~LocalAgentDocumentCompression.document_id.in_(sorted(queue_blocked_doc_ids))
        )
    if prompt_version_value:
        terminal_for_prompt = aliased(LocalAgentDocumentCompression)
        terminal_for_prompt_doc_ids = (
            session.query(terminal_for_prompt.document_id)
            .filter(terminal_for_prompt.document_id.isnot(None))
            .filter(terminal_for_prompt.status.in_(["compressed", "attached", "written", "skipped", "noop", "error"]))
            .filter(terminal_for_prompt.prompt_version == prompt_version_value[:64])
        )
        retry_signal_query = retry_signal_query.filter(
            ~LocalAgentDocumentCompression.document_id.in_(terminal_for_prompt_doc_ids)
        )
    retry_signal = retry_signal_query.distinct().count()
    durable_candidates = (
        session.query(LocalAgentEntityRoute)
        .filter(LocalAgentEntityRoute.lane == "durable")
        .filter(LocalAgentEntityRoute.durable_relevance.is_(True))
        .count()
    )
    batch_plan, lookback_expansion = _adaptive_stage_signal_batch_plan(
        session,
        signal_quota=signal_quota,
        lookback_hours=lookback_hours,
        queue_strategy=strategy,
        now=now,
        prompt_version=prompt_version,
    )
    deferred_pending_media = int(batch_plan.get("documents_deferred_pending_media") or 0)
    deferred_pending_media_ids = [
        int(value)
        for value in _json_list(batch_plan.get("documents_deferred_pending_media_ids"))
        if str(value).isdigit()
    ][:50]
    if requested_document_ids:
        cohort_docs = (
            session.query(Document)
            .options(joinedload(Document.source), joinedload(Document.attachments))
            .filter(Document.id.in_(sorted(active_document_ids)))
            .all()
        )
        deferred_pending_media_ids = [
            int(doc.id)
            for doc in cohort_docs
            if any(entry.get("is_signal") is None for entry in _document_media_context(doc, max_chars=0)[1])
        ]
        deferred_pending_media = len(deferred_pending_media_ids)
    pending_media = max(actionable_media_jobs, deferred_pending_media)
    signal_lookback_hours = int(lookback_expansion.get("effective_lookback_hours") or batch_plan.get("lookback_hours") or 1)
    backlog_after_fresh = {
        "enabled": True,
        "activated": False,
        "reason": "fresh_window_has_work",
        "lookback_hours": None,
    }
    if pending_media == 0 and int(batch_plan.get("documents_planned") or 0) == 0:
        try:
            requested_hours = max(1, int(lookback_hours or getattr(settings, "agent_lookback_hours", 48) or 48))
        except Exception:
            requested_hours = 48
        backlog_hours = min(LOCAL_AGENT_MAX_QUEUE_LOOKBACK_HOURS, max(requested_hours * 15, 24 * 30))
        backlog_plan = get_document_batch_plan(
            session,
            limit=signal_quota,
            lookback_hours=backlog_hours,
            include_filings=False,
            include_opened=False,
            defer_pending_media=True,
            max_input_tokens=32_000,
            max_documents_per_batch=min(6, signal_quota),
            queue_strategy="oldest",
            prompt_version=prompt_version,
        )
        backlog_after_fresh = {
            "enabled": True,
            "activated": bool(int(backlog_plan.get("documents_planned") or 0) > 0),
            "reason": "fresh_window_complete" if int(backlog_plan.get("documents_planned") or 0) > 0 else "fresh_window_complete_no_backlog",
            "lookback_hours": int(backlog_plan.get("lookback_hours") or backlog_hours),
        }
        if backlog_after_fresh["activated"]:
            batch_plan = backlog_plan
            signal_lookback_hours = int(backlog_plan.get("lookback_hours") or backlog_hours)
            lookback_expansion = {
                **lookback_expansion,
                "backlog_after_fresh": backlog_after_fresh,
                "effective_lookback_hours": signal_lookback_hours,
                "expanded": True,
                "reason": "fresh_window_complete_backlog_frontier",
            }
    else:
        lookback_expansion = {**lookback_expansion, "backlog_after_fresh": backlog_after_fresh}
    stages = [
        {
            "stage": "media_prep",
            "quota": min(media_quota, int(pending_media or 0)) if pending_media else 0,
            "pending_jobs": int(pending_media or 0),
            "actionable_jobs": int(actionable_media_jobs or 0),
            "pending_work": int(pending_media or 0),
            "unresolved_documents": int(deferred_pending_media or 0),
            "unresolved_document_ids": deferred_pending_media_ids,
            "running_jobs": int(running_media or 0),
            "error_jobs": int(error_media or 0),
            "active_window_documents": len(active_document_ids),
            "ocr_pending_jobs": int(active_media_counts.get("ocr_pending_jobs") or 0),
            "vlm_pending_jobs": int(active_media_counts.get("vlm_pending_jobs") or 0),
            "retryable_error_jobs": int(active_media_counts.get("retryable_error_jobs") or 0),
            "retry_delayed_error_jobs": int(active_media_counts.get("retry_delayed_error_jobs") or 0),
            "exhausted_error_jobs": int(active_media_counts.get("exhausted_error_jobs") or 0),
            "status_counts": media_status_counts,
            "rail": "media_transcription",
        },
        {
            "stage": "signal_extraction",
            "quota": signal_quota,
            "lookback_hours": signal_lookback_hours,
            "planned_documents": int(batch_plan.get("documents_planned") or 0),
            "documents_deferred_pending_media": int(deferred_pending_media or 0),
            "documents_deferred_pending_media_ids": deferred_pending_media_ids,
            "ready_documents_estimate": int(batch_plan.get("documents_planned") or 0),
            "retry_pending_compressions": int(retry_signal or 0),
            "retry_exhausted_documents": len(retry_exhausted_doc_ids),
            "retry_exhausted_stage_failure_documents": len(stage_failure_exhausted_doc_ids),
            "retry_exhausted_read_documents": len(read_exhausted_doc_ids),
            "prep_deferred_cooldown_documents": len(prep_deferred_cooldown_doc_ids),
            "rail": "document_batch_plan",
        },
        {
            "stage": "durable_reasoning",
            "quota": min(durable_quota, int(durable_candidates or 0)) if durable_candidates else durable_quota,
            "candidate_routes": int(durable_candidates or 0),
            "rail": "decision_context+durable_context",
        },
    ]
    return {
        "created_at": _dt(now),
        "lookback_hours": batch_plan.get("lookback_hours"),
        "signal_lookback_hours": signal_lookback_hours,
        "lookback_expansion": lookback_expansion,
        "backlog_after_fresh": backlog_after_fresh,
        "queue_strategy": strategy,
        "prompt_version": str(prompt_version or "").strip() or None,
        "explicit_document_cohort": bool(requested_document_ids),
        "requested_document_ids": requested_document_ids,
        "active_document_ids": sorted(active_document_ids) if requested_document_ids else None,
        "balance_rule": "Run bounded media prep first, then bounded signal extraction with adaptive tail lookback, then durable reasoning only from extracted candidates.",
        "stages": stages,
        "batch_plan": {
            "documents_planned": batch_plan.get("documents_planned"),
            "planned_batches": len(_json_list(batch_plan.get("batches"))),
            "documents_deferred_pending_media": batch_plan.get("documents_deferred_pending_media"),
            "documents_deferred_pending_media_ids": deferred_pending_media_ids,
            "document_ids": _batch_plan_document_ids(batch_plan),
        },
    }


def _stage_plan_row(plan: dict[str, Any], stage_name: str) -> dict[str, Any]:
    for row in _json_list(plan.get("stages")):
        if isinstance(row, dict) and str(row.get("stage") or "").strip().lower() == stage_name:
            return row
    return {}


def _run_stat_int(stats: dict[str, Any], key: str) -> int:
    try:
        return int(stats.get(key) or 0)
    except Exception:
        return 0


def _run_stat_bool(stats: dict[str, Any], key: str) -> bool:
    value = stats.get(key)
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


def _local_agent_run_stats(row: LocalAgentRun) -> dict[str, Any]:
    return dict(row.stats_json or {}) if isinstance(row.stats_json, dict) else {}


def _text_run_media_blocked(row: LocalAgentRun) -> bool:
    if str(row.run_type or "").strip() != "feed_triage":
        return False
    if str(row.status or "").strip().lower() not in {"success", "stopped"}:
        return False
    stats = _local_agent_run_stats(row)
    if _run_stat_bool(stats, "media_transcription_enabled"):
        return False
    return bool(
        _run_stat_int(stats, "documents") == 0
        and _run_stat_int(stats, "compressions") == 0
        and _run_stat_int(stats, "media_jobs_claimed") == 0
        and _run_stat_int(stats, "media_jobs_completed") == 0
        and (
            _run_stat_int(stats, "documents_deferred_pending_prep") > 0
            or _run_stat_int(stats, "stage_plan_deferred_pending_media") > 0
        )
    )


def _serialize_stage_decision_run(row: LocalAgentRun) -> dict[str, Any]:
    stats = _local_agent_run_stats(row)
    return {
        "id": int(row.id),
        "run_type": row.run_type,
        "status": row.status,
        "started_at": _dt(row.started_at),
        "completed_at": _dt(row.completed_at),
        "media_transcription_enabled": _run_stat_bool(stats, "media_transcription_enabled"),
        "documents": _run_stat_int(stats, "documents"),
        "compressions": _run_stat_int(stats, "compressions"),
        "planned_documents": _run_stat_int(stats, "planned_documents"),
        "documents_deferred_pending_prep": _run_stat_int(stats, "documents_deferred_pending_prep"),
        "stage_plan_pending_media": _run_stat_int(stats, "stage_plan_pending_media"),
        "stage_plan_deferred_pending_media": _run_stat_int(stats, "stage_plan_deferred_pending_media"),
        "media_jobs_claimed": _run_stat_int(stats, "media_jobs_claimed"),
        "media_jobs_completed": _run_stat_int(stats, "media_jobs_completed"),
    }


def _stage_decision_error_code(reason: str, *, recommendation: str, running_media: int = 0) -> str | None:
    normalized_reason = re.sub(r"[^a-z0-9_]+", "_", str(reason or "").strip().lower()).strip("_")
    normalized_recommendation = re.sub(r"[^a-z0-9_]+", "_", str(recommendation or "").strip().lower()).strip("_")
    if running_media > 0:
        return "media_rotation_in_progress"
    if normalized_reason in {"media_retry_delayed_before_text_lane", "media_retry_delayed_no_text_work"}:
        return "media_retry_delayed"
    if normalized_reason == "media_prep_blocked_no_actionable_jobs":
        return "media_prep_blocked_no_actionable_jobs"
    if normalized_recommendation == "rotate_media_prep":
        return normalized_reason or "media_prep_required"
    if normalized_recommendation == "wait":
        return normalized_reason or "runtime_wait"
    return None


def local_agent_runtime_stage_decision(
    session: Session,
    *,
    media_limit: int = 2,
    signal_limit: int = 4,
    durable_limit: int = 2,
    lookback_hours: int | None = None,
    queue_strategy: str = "newest",
    prompt_version: str | None = None,
    recent_run_limit: int = 8,
    min_consecutive_text_zero_runs: int = 2,
    document_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Read-only runtime-stage recommendation for the remote model owner.

    This does not rotate anything itself. It promotes a text-only run pattern
    that repeatedly sees only media-blocked documents into an explicit media
    prep recommendation so the ops rail can unload the 35B and run OCR/VLM.
    """
    plan = local_agent_stage_plan(
        session,
        media_limit=media_limit,
        signal_limit=signal_limit,
        durable_limit=durable_limit,
        lookback_hours=lookback_hours,
        queue_strategy=queue_strategy,
        prompt_version=prompt_version,
        document_ids=document_ids,
    )
    media_stage = _stage_plan_row(plan, "media_prep")
    signal_stage = _stage_plan_row(plan, "signal_extraction")
    pending_media = int(media_stage.get("pending_work") or media_stage.get("pending_jobs") or 0)
    actionable_media = int(media_stage.get("actionable_jobs") or 0)
    ocr_pending_media = int(media_stage.get("ocr_pending_jobs") or 0)
    vlm_pending_media = int(media_stage.get("vlm_pending_jobs") or 0)
    running_media = int(media_stage.get("running_jobs") or 0)
    error_media = int(media_stage.get("error_jobs") or 0)
    retry_delayed_media = int(media_stage.get("retry_delayed_error_jobs") or 0)
    planned_documents = int(signal_stage.get("planned_documents") or 0)
    deferred_documents = int(signal_stage.get("documents_deferred_pending_media") or 0)
    deferred_document_ids = [
        int(value)
        for value in _json_list(signal_stage.get("documents_deferred_pending_media_ids") or media_stage.get("unresolved_document_ids"))
        if str(value).isdigit()
    ][:50]
    ready_documents = int(signal_stage.get("ready_documents_estimate") or max(0, planned_documents - deferred_documents))
    backlog_after_fresh = plan.get("backlog_after_fresh") if isinstance(plan.get("backlog_after_fresh"), dict) else {}
    fresh_window_complete = bool(
        pending_media == 0
        and running_media == 0
        and (
            str(backlog_after_fresh.get("reason") or "").startswith("fresh_window_complete")
            or (planned_documents == 0 and ready_documents == 0)
        )
    )

    resolved_recent_limit = max(1, min(int(recent_run_limit or 8), 50))
    runs = (
        session.query(LocalAgentRun)
        .filter(LocalAgentRun.run_type.in_(["feed_triage", "durable_review"]))
        .order_by(LocalAgentRun.id.desc())
        .limit(resolved_recent_limit)
        .all()
    )
    recent_runs = [_serialize_stage_decision_run(row) for row in runs]
    consecutive_text_media_blocked = 0
    for row in runs:
        if str(row.run_type or "").strip() != "feed_triage":
            continue
        if _text_run_media_blocked(row):
            consecutive_text_media_blocked += 1
            continue
        break

    min_runs = max(1, min(int(min_consecutive_text_zero_runs or 2), 10))
    obvious_media_block = bool(pending_media > 0 and deferred_documents > 0 and ready_documents == 0)
    repeated_text_block = bool(pending_media > 0 and consecutive_text_media_blocked >= min_runs)

    retry_delayed_text_allowed = bool(
        pending_media > 0
        and actionable_media <= 0
        and retry_delayed_media > 0
        and ready_documents > 0
    )

    if running_media > 0:
        recommendation = "wait"
        recommended_stage = "media_prep"
        recommended_model_stage = "media_prep"
        reason = "media_prep_already_running"
    elif retry_delayed_text_allowed:
        recommendation = "stay_text"
        recommended_stage = "text"
        recommended_model_stage = "text"
        reason = "media_retry_delayed_text_lane_available"
    elif pending_media > 0 and actionable_media <= 0 and ready_documents > 0:
        recommendation = "stay_text"
        recommended_stage = "text"
        recommended_model_stage = "text"
        reason = "media_deferred_text_lane_available"
    elif pending_media > 0 and actionable_media <= 0 and retry_delayed_media > 0:
        recommendation = "wait"
        recommended_stage = "media_prep"
        recommended_model_stage = "media_prep"
        reason = "media_retry_delayed_no_text_work"
    elif pending_media > 0 and actionable_media <= 0 and (obvious_media_block or repeated_text_block):
        recommendation = "rotate_media_prep"
        recommended_stage = "media_prep"
        recommended_model_stage = "ocr"
        reason = "documents_deferred_pending_media_without_claimed_jobs"
    elif pending_media > 0 and ocr_pending_media > 0:
        recommendation = "rotate_media_prep"
        recommended_stage = "media_prep"
        recommended_model_stage = "ocr"
        reason = "media_prep_pending_before_text_lane"
    elif pending_media > 0 and vlm_pending_media > 0:
        recommendation = "rotate_media_prep"
        recommended_stage = "media_prep"
        recommended_model_stage = "vlm"
        reason = "vlm_prep_pending_before_text_lane"
    elif (obvious_media_block or repeated_text_block) and ocr_pending_media > 0:
        recommendation = "rotate_media_prep"
        recommended_stage = "media_prep"
        recommended_model_stage = "ocr"
        reason = "text_only_lane_has_only_media_blocked_documents"
    elif (obvious_media_block or repeated_text_block) and vlm_pending_media > 0:
        recommendation = "rotate_media_prep"
        recommended_stage = "media_prep"
        recommended_model_stage = "vlm"
        reason = "text_only_lane_has_only_vlm_blocked_documents"
    elif pending_media > 0 and planned_documents == 0:
        recommendation = "rotate_media_prep"
        recommended_stage = "media_prep"
        recommended_model_stage = "ocr" if ocr_pending_media > 0 else "vlm"
        reason = "no_text_signal_documents_and_media_prep_pending"
    elif deferred_documents > 0 and ready_documents == 0:
        recommendation = "rotate_media_prep"
        recommended_stage = "media_prep"
        recommended_model_stage = "ocr"
        reason = "documents_deferred_pending_media_without_claimed_jobs"
    else:
        recommendation = "stay_text"
        recommended_stage = "text"
        recommended_model_stage = "text"
        reason = "text_lane_has_ready_or_non_media_work"
    error_code = _stage_decision_error_code(reason, recommendation=recommendation, running_media=running_media)
    media_state_code = (
        "media_rotation_in_progress"
        if running_media > 0
        else "media_actionable"
        if actionable_media > 0
        else "media_retry_delayed_text_allowed"
        if retry_delayed_text_allowed
        else "media_retry_delayed"
        if retry_delayed_media > 0
        else "media_idle"
    )

    return {
        "created_at": _dt(utcnow()),
        "recommendation": recommendation,
        "recommended_stage": recommended_stage,
        "recommended_model_stage": recommended_model_stage,
        "reason": reason,
        "error_code": error_code,
        "blocked_code": error_code,
        "media_state_code": media_state_code,
        "text_starved_runs": int(consecutive_text_media_blocked),
        "min_consecutive_text_zero_runs": int(min_runs),
        "pending_media_jobs": int(pending_media),
        "media_actionable_jobs": int(actionable_media),
        "media_ocr_pending_jobs": int(ocr_pending_media),
        "media_vlm_pending_jobs": int(vlm_pending_media),
        "running_media_jobs": int(running_media),
        "error_media_jobs": int(error_media),
        "media_retry_delayed_jobs": int(retry_delayed_media),
        "media_retry_delayed_nonblocking": bool(retry_delayed_text_allowed),
        "model_work_state": (
            "media_running"
            if running_media > 0
            else "media_actionable"
            if actionable_media > 0
            else "retry_delayed_text_available"
            if retry_delayed_text_allowed
            else "deferred_media_text_available"
            if pending_media > 0 and actionable_media <= 0 and ready_documents > 0
            else "retry_delayed"
            if retry_delayed_media > 0
            else "text_available"
            if ready_documents > 0
            else "idle"
        ),
        "active_window_documents": int(media_stage.get("active_window_documents") or 0),
        "planned_signal_documents": int(planned_documents),
        "deferred_signal_documents": int(deferred_documents),
        "deferred_signal_document_ids": deferred_document_ids,
        "media_unresolved_document_ids": deferred_document_ids,
        "ready_signal_documents_estimate": int(ready_documents),
        "fresh_window_complete": fresh_window_complete,
        "backlog_after_fresh": backlog_after_fresh,
        "media_quota": int(media_stage.get("quota") or 0),
        "signal_quota": int(signal_stage.get("quota") or 0),
        "durable_quota": int(_stage_plan_row(plan, "durable_reasoning").get("quota") or 0),
        "stage_plan": plan,
        "recent_runs": recent_runs,
    }


def _grouped_counts(rows: Any, *columns: Any, limit: int = 20) -> list[dict[str, Any]]:
    count_expr = func.count(Document.id)
    out: list[dict[str, Any]] = []
    for item in rows.with_entities(*columns, count_expr).group_by(*columns).order_by(count_expr.desc()).limit(limit).all():
        values = list(item[:-1])
        out.append(
            {
                "values": [str(value) if value is not None else None for value in values],
                "count": int(item[-1] or 0),
            }
        )
    return out


def get_document_backlog_plan(
    session: Session,
    *,
    lookback_hours: int | None = None,
    include_filings: bool = False,
    include_opened: bool = True,
    include_product_snapshots: bool = False,
    sample_limit: int = 12,
    batch_size: int = 4,
) -> dict[str, Any]:
    resolved_sample_limit = _limit(sample_limit, default=12, high=50)
    resolved_batch_size = _limit(batch_size, default=4, high=50)
    rows, hours, cutoff, priority = _agent_queue_scope(
        session,
        lookback_hours=lookback_hours,
        include_filings=include_filings,
        include_opened=include_opened,
        include_product_snapshots=include_product_snapshots,
    )
    total = int(rows.with_entities(func.count(Document.id)).scalar() or 0)
    oldest_at, newest_at = rows.with_entities(
        func.min(func.coalesce(Document.published_at, Document.ingested_at)),
        func.max(func.coalesce(Document.published_at, Document.ingested_at)),
    ).one()
    sample_rows = (
        rows.options(joinedload(Document.source))
        .order_by(*_agent_queue_order(priority))
        .limit(resolved_sample_limit)
        .all()
    )
    return {
        "lookback_hours": hours,
        "cutoff_at": _dt(cutoff),
        "include_opened": bool(include_opened),
        "include_filings": bool(include_filings),
        "include_product_snapshots": bool(include_product_snapshots),
        "sample_limit": resolved_sample_limit,
        "batch_size": resolved_batch_size,
        "total_documents": total,
        "estimated_batches": (total + resolved_batch_size - 1) // resolved_batch_size if total else 0,
        "oldest_document_at": _dt(oldest_at),
        "newest_document_at": _dt(newest_at),
        "by_triage_state": [
            {"triage_state": item["values"][0] or "UNKNOWN", "count": item["count"]}
            for item in _grouped_counts(rows, Document.triage_state)
        ],
        "by_kind": [
            {"kind": item["values"][0] or "unknown", "count": item["count"]}
            for item in _grouped_counts(rows, Document.kind)
        ],
        "by_source": [
            {"source_name": item["values"][0] or "unknown", "source_category": item["values"][1], "count": item["count"]}
            for item in _grouped_counts(rows, Source.name, Source.category)
        ],
        "sample_documents": [_document_queue_preview(row) for row in sample_rows],
    }


def record_document_read(session: Session, *, document_id: int, run_id: int | None = None, read_kind: str = "full") -> LocalAgentDocumentRead:
    row = LocalAgentDocumentRead(
        run_id=int(run_id) if run_id is not None else None,
        document_id=int(document_id),
        read_kind=str(read_kind or "full").strip()[:32],
        created_at=utcnow(),
    )
    session.add(row)
    _upsert_document_stage_state(
        session,
        document_id=int(document_id),
        stage="read",
        status="read",
        run_id=run_id,
        payload={"read_kind": str(read_kind or "full").strip()[:32]},
    )
    session.flush()
    return row


def get_document_for_agent(
    session: Session,
    *,
    document_id: int,
    run_id: int | None = None,
    max_text_chars: int = 24000,
) -> dict[str, Any]:
    doc = session.query(Document).options(joinedload(Document.source)).filter_by(id=int(document_id)).first()
    if doc is None:
        raise ValueError("Document not found.")
    if not _agent_can_access_document(session, int(doc.id)):
        raise ValueError("Document is outside active-universe agent intake scope.")
    sec_hydration = maybe_hydrate_sec_filing_document(session, doc)
    transcript_hydration = maybe_hydrate_transcript_document(session, doc)
    pdf_hydration = maybe_hydrate_pdf_attachment_text(session, doc)
    ensure_pdf_page_image_attachments(session, doc)
    if not any(
        row.get("status") == "ok"
        for row in (sec_hydration, transcript_hydration, pdf_hydration)
        if isinstance(row, dict)
    ):
        maybe_hydrate_url_document_text(session, doc)
    if len(str(doc.translation_en or doc.normalized_text or doc.raw_text or "").strip()) >= 200:
        _mark_text_ready_compression_retries(
            session,
            document_id=int(doc.id),
            now=utcnow(),
            reason=TEXT_READY_RETRY_REASON,
        )
    ensure_agent_document_translation(session, doc)
    record_document_read(session, document_id=int(doc.id), run_id=run_id, read_kind="full")
    session.flush()
    session.expire(doc, ["attachments"])
    return document_payload(doc, max_text_chars=max_text_chars)


def list_watchlists(session: Session) -> list[dict[str, Any]]:
    rows = (
        session.query(ResearchUniverse)
        .filter(ResearchUniverse.status != "archived")
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverse.name.asc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for universe in rows:
        row_items = (
            session.query(ResearchUniverseRow)
            .options(joinedload(ResearchUniverseRow.instrument), joinedload(ResearchUniverseRow.section))
            .filter(ResearchUniverseRow.universe_id == int(universe.id), ResearchUniverseRow.archived_at.is_(None))
            .order_by(ResearchUniverseRow.sort_order.asc(), ResearchUniverseRow.display_label.asc())
            .limit(500)
            .all()
        )
        out.append(
            {
                "id": int(universe.id),
                "slug": universe.slug,
                "name": universe.name,
                "description": universe.description,
                "rows": [
                    {
                        "id": int(row.id),
                        "display_label": row.display_label,
                        "role": row.role,
                        "exposure_type": row.exposure_type,
                        "section": row.section.name if row.section else None,
                        "instrument": {
                            "id": int(row.instrument.id),
                            "display_name": row.instrument.display_name,
                            "ticker": row.instrument.ticker,
                            "is_public": bool(row.instrument.is_public),
                            "company_id": row.instrument.company_id,
                            "issuer_id": row.instrument.issuer_id,
                        }
                        if row.instrument
                        else None,
                    }
                    for row in row_items
                ],
            }
        )
    return out


def search_entities(session: Session, *, query: str, limit: int = 12, allow_provider_fallback: bool = True) -> list[dict[str, Any]]:
    q = _clean(query)
    if not q:
        return []
    lim = _limit(limit, default=12, high=50)
    pattern = f"%{q}%"
    query_key = canonical_entity_key(q)
    query_tokens = {token for token in query_key.split() if len(token) > 1}
    out: list[dict[str, Any]] = []

    def add(item: dict[str, Any]) -> None:
        key = (str(item.get("kind")), str(item.get("id")), str(item.get("label")))
        if not any((str(v.get("kind")), str(v.get("id")), str(v.get("label"))) == key for v in out):
            out.append(item)

    instruments = (
        session.query(ResearchInstrument)
        .filter(
            or_(
                ResearchInstrument.display_name.ilike(pattern),
                ResearchInstrument.legal_name.ilike(pattern),
                ResearchInstrument.ticker.ilike(pattern),
                ResearchInstrument.instrument_key.ilike(pattern),
            )
        )
        .order_by(ResearchInstrument.display_name.asc())
        .limit(lim)
        .all()
    )
    private_keys = [f"private_instrument:{int(row.id)}" for row in instruments if not row.is_public]
    private_subject_ids = {
        row.subject_key: int(row.id)
        for row in session.query(ResearchSubject).filter(
            ResearchSubject.status == "active", ResearchSubject.subject_key.in_(private_keys)
        ).all()
    } if private_keys else {}
    for row in instruments:
        add(
            {
                "kind": "research_instrument",
                "id": int(row.id),
                "label": row.display_name,
                "subject_id": private_subject_ids.get(f"private_instrument:{int(row.id)}"),
                "ticker": row.ticker,
                "exchange": row.exchange,
                "currency": row.currency,
                "security_type": row.security_type,
                "company_id": row.company_id,
                "issuer_id": row.issuer_id,
                "is_public": bool(row.is_public),
                "asset_class": (row.metadata_json or {}).get("asset_class")
                or ((row.metadata_json or {}).get("ibkr_watchlist_item") or {}).get("assetClass"),
            }
        )
    for row in (
        session.query(ResearchSubject)
        .outerjoin(ResearchSubjectAlias, ResearchSubjectAlias.subject_id == ResearchSubject.id)
        .filter(
            or_(
                ResearchSubject.display_name.ilike(pattern),
                ResearchSubject.legal_name.ilike(pattern),
                ResearchSubject.slug.ilike(pattern),
                ResearchSubject.subject_key.ilike(pattern),
                ResearchSubjectAlias.alias.ilike(pattern),
            )
        )
        .order_by(ResearchSubject.updated_at.desc())
        .limit(lim)
        .all()
    ):
        add(
            {
                "kind": "research_subject",
                "id": int(row.id),
                "subject_id": int(row.id),
                "label": row.display_name,
                "slug": row.slug,
                "subject_key": row.subject_key,
                "company_id": row.company_id,
                "issuer_id": row.issuer_id,
            }
        )
    for row in (
        session.query(Company)
        .filter(or_(Company.name.ilike(pattern), Company.slug.ilike(pattern), Company.ticker.ilike(pattern)))
        .order_by(Company.name.asc())
        .limit(lim)
        .all()
    ):
        add({"kind": "company", "id": int(row.id), "label": row.name, "slug": row.slug, "ticker": row.ticker})
    for row in (
        session.query(Issuer)
        .filter(or_(Issuer.display_name.ilike(pattern), Issuer.legal_name.ilike(pattern), Issuer.slug.ilike(pattern)))
        .order_by(Issuer.display_name.asc())
        .limit(lim)
        .all()
    ):
        add({"kind": "issuer", "id": int(row.id), "label": row.display_name, "slug": row.slug})

    def fuzzy_candidate_values(*values: object) -> bool:
        if not query_key:
            return False
        for value in values:
            raw = str(value or "").strip()
            if not raw:
                continue
            key = canonical_entity_key(raw)
            if not key:
                continue
            if query_key in key or key in query_key:
                return True
            if name_match_score(query_key, key) >= 0.72:
                return True
            tokens = {token for token in key.split() if len(token) > 1}
            if query_tokens and tokens and len(query_tokens & tokens) / max(1, min(len(query_tokens), len(tokens))) >= 0.8:
                return True
        return False

    def provider_candidate_values(*values: object) -> bool:
        if len(query_tokens) == 1:
            query_token = next(iter(query_tokens))
            for value in values:
                key = canonical_entity_key(value)
                tokens = {token for token in key.split() if len(token) > 1}
                if key == query_token or query_token in tokens:
                    return True
            return False
        return fuzzy_candidate_values(*values)

    if len(out) < lim:
        for row in session.query(ResearchInstrument).order_by(ResearchInstrument.updated_at.desc()).limit(500).all():
            if fuzzy_candidate_values(row.display_name, row.legal_name, row.ticker, row.instrument_key):
                add(
                    {
                        "kind": "research_instrument",
                        "id": int(row.id),
                        "label": row.display_name,
                        "ticker": row.ticker,
                        "exchange": row.exchange,
                        "currency": row.currency,
                        "security_type": row.security_type,
                        "company_id": row.company_id,
                        "issuer_id": row.issuer_id,
                        "is_public": bool(row.is_public),
                        "asset_class": (row.metadata_json or {}).get("asset_class")
                        or ((row.metadata_json or {}).get("ibkr_watchlist_item") or {}).get("assetClass"),
                        "match": "canonical",
                    }
                )
            if len(out) >= lim:
                break
    if len(out) < lim:
        for row in session.query(Company).order_by(Company.id.desc()).limit(500).all():
            if fuzzy_candidate_values(row.name, row.slug, row.ticker):
                add({"kind": "company", "id": int(row.id), "label": row.name, "slug": row.slug, "ticker": row.ticker, "match": "canonical"})
            if len(out) >= lim:
                break
    if len(out) < lim:
        for row in session.query(Issuer).order_by(Issuer.id.desc()).limit(500).all():
            if fuzzy_candidate_values(row.display_name, row.legal_name, row.slug):
                add({"kind": "issuer", "id": int(row.id), "label": row.display_name, "slug": row.slug, "match": "canonical"})
            if len(out) >= lim:
                break
    if allow_provider_fallback and not out:
        try:
            yahoo_rows = search_symbols(q, user_agent=settings.discord_market_user_agent, timeout_s=5.0, limit=min(lim, 5))
        except Exception:
            yahoo_rows = []
        for row in yahoo_rows:
            symbol = _clean(row.symbol).upper()
            if not symbol:
                continue
            if not provider_candidate_values(row.name, symbol):
                continue
            add(
                {
                    "kind": "symbol_candidate",
                    "id": f"yahoo:{symbol}",
                    "label": row.name or symbol,
                    "ticker": symbol,
                    "exchange": row.exchange,
                    "is_public": True,
                    "provider": "yahoo",
                    "match": "provider_fallback",
                }
            )
            if len(out) >= lim:
                break
    return out[:lim]


def search_research_docs(session: Session, *, query: str, limit: int = 12) -> list[dict[str, Any]]:
    q = _clean(query)
    if not q:
        return []
    lim = _limit(limit, default=12, high=50)
    pattern = f"%{q}%"
    out: list[dict[str, Any]] = []
    for row in (
        session.query(ResearchReport)
        .filter(or_(ResearchReport.title.ilike(pattern), ResearchReport.slug.ilike(pattern), ResearchReport.subject_key.ilike(pattern)))
        .order_by(ResearchReport.updated_at.desc())
        .limit(lim)
        .all()
    ):
        out.append({"kind": "research_report", "id": int(row.id), "title": row.title, "slug": row.slug, "subject_key": row.subject_key})
    for row in (
        session.query(KnowledgeAsset)
        .filter(or_(KnowledgeAsset.title.ilike(pattern), KnowledgeAsset.slug.ilike(pattern), KnowledgeAsset.summary.ilike(pattern)))
        .order_by(KnowledgeAsset.updated_at.desc())
        .limit(lim)
        .all()
    ):
        out.append({"kind": "knowledge_asset", "id": int(row.id), "title": row.title, "slug": row.slug, "asset_kind": row.kind})
    for row in (
        session.query(SupplyChain)
        .filter(or_(SupplyChain.title.ilike(pattern), SupplyChain.slug.ilike(pattern), SupplyChain.product.ilike(pattern), SupplyChain.description.ilike(pattern)))
        .order_by(SupplyChain.updated_at.desc())
        .limit(lim)
        .all()
    ):
        out.append({"kind": "supply_chain", "id": int(row.id), "title": row.title, "slug": row.slug, "product": row.product})
    return out[:lim]


def _trim_agent_object(value: object, *, depth: int = 0, path: tuple[str, ...] = ()) -> object:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.startswith("_") or _agent_context_should_redact_key(key_text, path):
                continue
            if depth > 4:
                out[key_text] = _shorten(_strip_agent_context_noise(item, path=(*path, key_text)), 300)
                continue
            out[key_text] = _trim_agent_object(item, depth=depth + 1, path=(*path, key_text))
        return out
    if isinstance(value, list):
        cap = 8 if depth <= 1 else 5
        if depth > 4:
            return [_shorten(_strip_agent_context_noise(item, path=path), 300) for item in value[:cap]]
        return [_trim_agent_object(item, depth=depth + 1, path=path) for item in value[:cap]]
    if depth > 4:
        return _shorten(value, 500)
    if isinstance(value, str):
        return _shorten(value, 1600 if depth <= 1 else 800)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def search_objects_for_agent(
    session: Session,
    *,
    query: str,
    kind: str = "all",
    limit: int = 12,
    include_neighborhoods: bool = False,
) -> dict[str, Any]:
    q = _clean(query)
    if not q:
        return {"query": "", "kind": kind or "all", "count": 0, "items": [], "groups": {}, "neighborhoods": []}
    from thoth_terminal.object_search import object_aware_search

    payload = object_aware_search(
        session,
        q,
        kind=str(kind or "all").strip() or "all",
        limit=_limit(limit, default=12, high=25),
        include_neighborhoods=bool(include_neighborhoods),
        include_hidden=False,
    )
    return _trim_agent_object(_strip_agent_context_noise(payload), path=("object_search",))  # type: ignore[return-value]


def get_research_doc(session: Session, *, destination_type: str, destination_id: int) -> dict[str, Any]:
    dtype = _destination_type(destination_type)
    if dtype == "research_report":
        report = session.query(ResearchReport).filter_by(id=int(destination_id)).first()
        if report is None:
            raise ValueError("Research report not found.")
        latest = (
            session.query(ResearchReportVersion)
            .filter_by(report_id=int(report.id), status="published")
            .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
            .first()
        )
        return {
            "destination_type": "research_report",
            "destination_id": int(report.id),
            "title": report.title,
            "version_id": int(latest.id) if latest else None,
            "content_md": latest.content_md if latest else "",
            "updated_at": _dt(report.updated_at),
        }
    if dtype == "knowledge_asset":
        asset = session.query(KnowledgeAsset).filter_by(id=int(destination_id)).first()
        if asset is None:
            raise ValueError("Knowledge asset not found.")
        latest = (
            session.query(KnowledgeAssetVersion)
            .filter_by(asset_id=int(asset.id), status="published")
            .order_by(KnowledgeAssetVersion.version.desc(), KnowledgeAssetVersion.id.desc())
            .first()
        )
        return {
            "destination_type": "knowledge_asset",
            "destination_id": int(asset.id),
            "title": asset.title,
            "version_id": int(latest.id) if latest else None,
            "content_md": latest.content_md if latest else "",
            "updated_at": _dt(asset.updated_at),
        }
    if dtype == "supply_chain":
        chain = session.query(SupplyChain).filter_by(id=int(destination_id)).first()
        if chain is None:
            raise ValueError("Supply chain not found.")
        data = dict(chain.data or {})
        return {
            "destination_type": "supply_chain",
            "destination_id": int(chain.id),
            "title": chain.title,
            "version_id": None,
            "content_md": str(data.get("agent_research_md") or ""),
            "updated_at": _dt(chain.updated_at),
        }
    raise ValueError(f"Destination type `{dtype}` has no document body.")


def _candidate_context_terms(payload: dict[str, Any], *, limit: int = 8) -> list[str]:
    terms: list[str] = []

    def add(value: object) -> None:
        text = _clean(value)
        if not text:
            return
        if len(text) > 140:
            text = text[:140].strip()
        if text and text.lower() not in {item.lower() for item in terms}:
            terms.append(text)

    for field in ("candidate_writes", "research_writes"):
        for row in _json_list(payload.get(field)):
            if isinstance(row, dict):
                add(row.get("subject"))
                add(row.get("write_summary"))
                add(row.get("new_information"))
    for row in _json_list(payload.get("durable_routes")):
        if isinstance(row, dict):
            add(row.get("subject") or row.get("entity_name"))
            add(row.get("new_information"))
    result = _json_dict(payload.get("result") or payload.get("compression"))
    for row in _json_list(result.get("decision_targets")):
        if isinstance(row, dict):
            add(row.get("subject") or row.get("company") or row.get("ticker"))
    for row in _json_list(result.get("analyst_evidence")):
        if isinstance(row, dict):
            add(row.get("source_name") or row.get("underlying_firm") or row.get("outlet"))
            add(row.get("summary"))
            add(row.get("metric_key") or row.get("estimate_metric"))
            add(row.get("estimate_period") or row.get("forecast_period"))
    for row in _json_list(result.get("model_delta_candidates")):
        if isinstance(row, dict):
            add(row.get("raw_subject_text") or row.get("canonical_subject_key") or row.get("subject"))
            add(row.get("summary") or row.get("rationale"))
            add(row.get("canonical_metric_key") or row.get("metric_key"))
            add(row.get("period") or row.get("forecast_period"))
            add(row.get("value_text"))
    for row in _json_list(result.get("entity_refs")):
        if isinstance(row, dict):
            add(row.get("name"))
    for row in _json_list(result.get("atomic_claims")):
        if isinstance(row, dict):
            add(row.get("claim_text"))
    doc = _json_dict(payload.get("document"))
    add(doc.get("title"))
    return terms[:limit]


def _candidate_payload_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add_many(source_name: str, values: object) -> None:
        for value in _json_list(values):
            if not isinstance(value, dict):
                continue
            row = dict(value)
            row["_candidate_source"] = source_name
            rows.append(row)

    for field in ("candidate_writes", "research_writes", "durable_routes", "summary_routes", "entity_routing", "decision_targets"):
        add_many(field, payload.get(field))
    result = _json_dict(payload.get("result") or payload.get("compression"))
    for field in (
        "decision_targets",
        "analyst_evidence",
        "model_delta_candidates",
        "catalysts",
        "investment_memos",
        "research_writes",
        "durable_routes",
        "entity_refs",
        "entity_routing",
    ):
        add_many(field, result.get(field))
    return rows


def _candidate_metric_context(payload: dict[str, Any], *, limit: int = 12) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in _candidate_payload_rows(payload):
        forecast = _json_dict(row.get("forecast"))
        metric_key = normalize_metric_key(
            row.get("canonical_metric_key")
            or row.get("metric_key")
            or row.get("estimate_metric")
            or forecast.get("metric_key")
        ) or _clean(row.get("canonical_metric_key") or row.get("metric_key") or row.get("estimate_metric") or forecast.get("metric_key"))
        period = _clean(row.get("period") or row.get("forecast_period") or row.get("estimate_period") or forecast.get("period"))
        value_number = _float_or_none(row.get("value_number") if row.get("value_number") is not None else row.get("forecast_value"))
        if value_number is None:
            value_number = _float_or_none(forecast.get("value") if forecast.get("value") is not None else forecast.get("forecast_value"))
        value_text = _clean(row.get("value_text") or row.get("forecast_status") or forecast.get("value_text") or forecast.get("forecast_status"))
        if not metric_key and not period and value_number is None and not value_text:
            continue
        owner_aliases = sorted(_source_owner_aliases(row))
        key = (metric_key.lower(), period.lower(), str(value_number if value_number is not None else value_text).lower(), "|".join(owner_aliases))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "metric_key": metric_key or None,
                "period": period or None,
                "value_number": value_number,
                "value_text": value_text or None,
                "estimate_owner": _clean(row.get("estimate_owner") or row.get("underlying_firm") or row.get("source_name")) or None,
                "owner_aliases": owner_aliases,
                "product_family": _clean(row.get("product_family") or forecast.get("product_family")) or None,
                "segment": _clean(row.get("segment") or forecast.get("segment")) or None,
                "scenario": _clean(row.get("scenario") or forecast.get("scenario")) or None,
                "source": row.get("_candidate_source"),
                "summary": _shorten(row.get("summary") or row.get("rationale"), 500),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _normalize_source_url(value: object) -> str:
    text = _clean(value)
    if not text:
        return ""
    text = re.sub(r"#.*$", "", text.strip())
    text = re.sub(r"\?.*$", "", text)
    return text.rstrip("/").lower()


def _candidate_source_context(
    session: Session,
    payload: dict[str, Any],
    *,
    document_id: int | None = None,
) -> dict[str, Any]:
    source_document_ids: set[int] = set()
    source_ids: set[int] = set()
    source_actor_ids: set[int] = set()
    source_urls: set[str] = set()
    source_names: set[str] = set()

    def add_int(target: set[int], value: object) -> None:
        parsed = _int(value)
        if parsed is not None:
            target.add(int(parsed))

    def add_url(value: object) -> None:
        normalized = _normalize_source_url(value)
        if normalized:
            source_urls.add(normalized)

    def add_name(value: object) -> None:
        name = _clean(value)
        if len(name) >= 3:
            source_names.add(name[:255])

    doc_payload = _json_dict(payload.get("document"))
    if document_id is not None:
        source_document_ids.add(int(document_id))
        doc = session.query(Document).filter_by(id=int(document_id)).first()
        if doc is not None:
            add_int(source_ids, doc.source_id)
            add_url(doc.url)
            add_url(doc.canonical_url)
            if doc.author:
                add_name(doc.author)
    add_int(source_document_ids, doc_payload.get("id") or doc_payload.get("document_id"))
    add_int(source_ids, doc_payload.get("source_id"))
    add_url(doc_payload.get("url"))
    add_url(doc_payload.get("canonical_url"))
    add_name(doc_payload.get("source_name"))
    add_name(doc_payload.get("author"))

    for row in _candidate_payload_rows(payload):
        add_int(source_document_ids, row.get("source_document_id") or row.get("document_id"))
        add_int(source_ids, row.get("source_id"))
        add_int(source_actor_ids, row.get("observed_source_actor_id"))
        add_int(source_actor_ids, row.get("underlying_source_actor_id"))
        add_url(row.get("source_url") or row.get("url") or row.get("canonical_url"))
        for field in ("source_name", "outlet", "author", "underlying_firm", "observed_source", "underlying_source"):
            add_name(row.get(field))
        attribution = _json_dict(row.get("source_attribution"))
        for key in ("observed_source", "underlying_source", "identity"):
            source_obj = _json_dict(attribution.get(key))
            add_int(source_actor_ids, source_obj.get("actor_id") or source_obj.get("source_actor_id") or source_obj.get("id"))
            add_url(source_obj.get("url") or source_obj.get("source_url") or source_obj.get("canonical_url"))
            for field in ("name", "display_name", "firm", "organization", "outlet", "handle"):
                add_name(source_obj.get(field))

    for name in list(source_names)[:16]:
        for alias_kind in ("name", "firm", "handle"):
            try:
                actor = find_source_actor_by_alias(session, alias=name, alias_kind=alias_kind)
            except Exception:
                actor = None
            if actor is not None:
                source_actor_ids.add(int(actor.id))
                break

    return {
        "source_document_ids": sorted(source_document_ids),
        "source_ids": sorted(source_ids),
        "source_actor_ids": sorted(source_actor_ids),
        "source_urls": sorted(source_urls),
        "source_names": sorted(source_names),
    }


def _candidate_identity_rows(
    session: Session,
    payload: dict[str, Any],
    *,
    terms: list[str],
    limit: int = 8,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def add_identity(kind: str, object_id: int | None, row: dict[str, Any]) -> None:
        if object_id is None:
            return
        key = (kind, int(object_id))
        if key in seen:
            return
        seen.add(key)
        rows.append(row)

    def subject_for(*, company_id: int | None = None, issuer_id: int | None = None) -> ResearchSubject | None:
        q = session.query(ResearchSubject)
        if company_id is not None:
            found = q.filter(ResearchSubject.company_id == int(company_id)).order_by(ResearchSubject.id.asc()).first()
            if found is not None:
                return found
        if issuer_id is not None:
            return q.filter(ResearchSubject.issuer_id == int(issuer_id)).order_by(ResearchSubject.id.asc()).first()
        return None

    def add_company(company: Company | None, *, match: str, source: object) -> None:
        if company is None:
            return
        subject = subject_for(company_id=int(company.id))
        add_identity(
            "company",
            int(company.id),
            {
                "kind": "company",
                "id": int(company.id),
                "label": company.name,
                "ticker": company.ticker,
                "company_id": int(company.id),
                "issuer_id": int(company.issuer_id) if company.issuer_id is not None else None,
                "subject_id": int(subject.id) if subject is not None else None,
                "subject_key": subject.subject_key if subject is not None else None,
                "match": match,
                "source": source,
            },
        )

    def add_issuer(issuer: Issuer | None, *, match: str, source: object) -> None:
        if issuer is None:
            return
        subject = subject_for(issuer_id=int(issuer.id))
        add_identity(
            "issuer",
            int(issuer.id),
            {
                "kind": "issuer",
                "id": int(issuer.id),
                "label": issuer.display_name,
                "issuer_id": int(issuer.id),
                "company_id": int(subject.company_id) if subject is not None and subject.company_id is not None else None,
                "subject_id": int(subject.id) if subject is not None else None,
                "subject_key": subject.subject_key if subject is not None else None,
                "match": match,
                "source": source,
            },
        )

    def add_subject(subject: ResearchSubject | None, *, match: str, source: object) -> None:
        if subject is None:
            return
        add_identity(
            "research_subject",
            int(subject.id),
            {
                "kind": "research_subject",
                "id": int(subject.id),
                "label": subject.display_name,
                "subject_id": int(subject.id),
                "subject_key": subject.subject_key,
                "company_id": int(subject.company_id) if subject.company_id is not None else None,
                "issuer_id": int(subject.issuer_id) if subject.issuer_id is not None else None,
                "match": match,
                "source": source,
            },
        )

    def add_site(site: Site | None, *, match: str, source: object) -> None:
        if site is None:
            return
        add_identity(
            "site",
            int(site.id),
            {
                "kind": "site",
                "id": int(site.id),
                "label": site.name,
                "site_id": int(site.id),
                "subject_id": int(site.subject_id) if site.subject_id is not None else None,
                "company_id": int(site.company_id) if site.company_id is not None else None,
                "issuer_id": int(site.issuer_id) if site.issuer_id is not None else None,
                "match": match,
                "source": source,
            },
        )

    def add_program(program: Program | None, *, match: str, source: object) -> None:
        if program is None:
            return
        add_identity(
            "program",
            int(program.id),
            {
                "kind": "program",
                "id": int(program.id),
                "label": program.title,
                "program_id": int(program.id),
                "site_id": int(program.site_id) if program.site_id is not None else None,
                "subject_id": int(program.subject_id) if program.subject_id is not None else None,
                "company_id": int(program.company_id) if program.company_id is not None else None,
                "issuer_id": int(program.issuer_id) if program.issuer_id is not None else None,
                "match": match,
                "source": source,
            },
        )

    names: list[tuple[str, object]] = []

    def add_name(value: object, source: object) -> None:
        name = _clean(value)
        if name and len(name) >= 3 and name.lower() not in {existing.lower() for existing, _ in names}:
            names.append((name, source))

    for row in _candidate_payload_rows(payload):
        source = row.get("_candidate_source")
        company_id = _int(row.get("company_id") or _json_dict(row.get("target")).get("company_id"))
        issuer_id = _int(row.get("issuer_id") or _json_dict(row.get("target")).get("issuer_id"))
        subject_id = _int(row.get("subject_id") or row.get("canonical_subject_id") or _json_dict(row.get("target")).get("subject_id"))
        site_id = _int(row.get("site_id") or _json_dict(row.get("target")).get("site_id"))
        program_id = _int(row.get("program_id") or _json_dict(row.get("target")).get("program_id"))
        if company_id is not None:
            add_company(session.get(Company, int(company_id)), match="explicit_company_id", source=source)
        if issuer_id is not None:
            add_issuer(session.get(Issuer, int(issuer_id)), match="explicit_issuer_id", source=source)
        if subject_id is not None:
            add_subject(session.get(ResearchSubject, int(subject_id)), match="explicit_subject_id", source=source)
        if site_id is not None:
            add_site(session.get(Site, int(site_id)), match="explicit_site_id", source=source)
        if program_id is not None:
            add_program(session.get(Program, int(program_id)), match="explicit_program_id", source=source)
        subject_key = _clean(row.get("canonical_subject_key") or row.get("subject_key"))
        if subject_key:
            add_subject(session.query(ResearchSubject).filter_by(subject_key=subject_key).first(), match="subject_key", source=source)
        ticker = _clean(row.get("ticker") or row.get("symbol") or _json_dict(row.get("target")).get("ticker")).upper().lstrip("$")
        if ticker:
            add_company(session.query(Company).filter(func.upper(Company.ticker) == ticker).first(), match="ticker", source=source)
            ident = (
                session.query(IssuerIdentifier)
                .filter(func.upper(IssuerIdentifier.id_type) == "TICKER", func.upper(IssuerIdentifier.id_value) == ticker)
                .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
                .first()
            )
            if ident is not None:
                add_issuer(session.get(Issuer, int(ident.issuer_id)), match="ticker", source=source)
        for field in ("subject", "entity_name", "name", "label", "company", "raw_subject_text", "site", "site_name", "program", "program_title"):
            add_name(row.get(field), source)

    for term in terms:
        add_name(term, "query_terms")

    for name, source in names[:12]:
        for company in session.query(Company).order_by(Company.id.desc()).limit(500).all():
            if name_match_score(name, company.name) >= 0.92 or name_match_score(name, company.slug) >= 0.95 or (company.ticker and name.upper().lstrip("$") == company.ticker.upper()):
                add_company(company, match="name", source=source)
                break
        for issuer in session.query(Issuer).order_by(Issuer.id.desc()).limit(500).all():
            if name_match_score(name, issuer.display_name) >= 0.92 or name_match_score(name, issuer.legal_name) >= 0.92 or name_match_score(name, issuer.slug) >= 0.95:
                add_issuer(issuer, match="name", source=source)
                break
        for match in _research_subject_matches(session, query=name, document=None, limit=3):
            subject_id = _int(match.get("id"))
            if subject_id is not None:
                add_subject(session.get(ResearchSubject, int(subject_id)), match="subject_search", source=source)
                break
        for site in session.query(Site).order_by(Site.id.desc()).limit(500).all():
            if name_match_score(name, site.name) >= 0.92 or name_match_score(name, site.slug) >= 0.95:
                add_site(site, match="name", source=source)
                break
        for program in session.query(Program).order_by(Program.id.desc()).limit(500).all():
            if name_match_score(name, program.title) >= 0.92 or name_match_score(name, program.slug) >= 0.95:
                add_program(program, match="name", source=source)
                break
        if len(rows) >= limit:
            break
    return rows[:limit]


def _candidate_identity_filters(identities: list[dict[str, Any]], model: Any) -> list[Any]:
    filters: list[Any] = []
    seen: set[tuple[str, int]] = set()
    for identity in identities:
        for field in ("subject_id", "company_id", "issuer_id", "site_id", "program_id", "source_id"):
            if not hasattr(model, field):
                continue
            value = _int(identity.get(field))
            if value is None:
                continue
            key = (field, int(value))
            if key in seen:
                continue
            seen.add(key)
            filters.append(getattr(model, field) == int(value))
    return filters


def _candidate_metric_sets(metrics: list[dict[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    metric_keys = {str(row.get("metric_key") or "").strip().lower() for row in metrics if row.get("metric_key")}
    periods = {str(row.get("period") or "").strip().lower() for row in metrics if row.get("period")}
    values: set[str] = set()
    for row in metrics:
        if row.get("value_number") is not None:
            values.add(str(row.get("value_number")).strip().lower())
        if row.get("value_text"):
            values.add(str(row.get("value_text")).strip().lower())
    return metric_keys, periods, values


def _owner_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def _source_owner_aliases(value: object) -> set[str]:
    aliases: set[str] = set()

    def add(raw: object) -> None:
        key = _owner_key(raw)
        if len(key) >= 2:
            aliases.add(key)

    if isinstance(value, dict):
        for field in (
            "estimate_owner",
            "underlying_firm",
            "underlying_source",
            "source_name",
            "source_owner",
            "source_firm",
            "research_firm",
            "institution",
            "firm",
            "outlet",
            "author",
        ):
            item = value.get(field)
            if isinstance(item, dict):
                for nested in ("firm", "name", "display_name", "organization", "source_name"):
                    add(item.get(nested))
            else:
                add(item)
        forecast = _json_dict(value.get("forecast") or value.get("projection"))
        if forecast:
            aliases.update(_source_owner_aliases(forecast))
        attribution = _json_dict(value.get("source_attribution"))
        for key in ("underlying_source", "observed_source", "identity"):
            source_obj = _json_dict(attribution.get(key))
            for field in ("firm", "name", "display_name", "organization", "outlet"):
                add(source_obj.get(field))
        nested_payload = _json_dict(value.get("payload"))
        if nested_payload and nested_payload is not value:
            for field in ("estimate_owner", "underlying_firm", "source_name"):
                add(nested_payload.get(field))
    else:
        add(value)
    return aliases


def _candidate_owner_aliases(metrics: list[dict[str, Any]]) -> set[str]:
    owners: set[str] = set()
    for row in metrics:
        for value in _json_list(row.get("owner_aliases")):
            owners.update(_source_owner_aliases(value))
        owners.update(_source_owner_aliases(row))
    return owners


def _owner_match(existing: set[str], incoming: set[str]) -> tuple[bool, bool]:
    existing_clean = {value for value in existing if value}
    incoming_clean = {value for value in incoming if value}
    if not existing_clean or not incoming_clean:
        return False, False
    overlap = bool(existing_clean & incoming_clean)
    return overlap, not overlap


def _existing_analyst_evidence_context(
    session: Session,
    *,
    identities: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    source_context: dict[str, Any],
    document_id: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    filters = _candidate_identity_filters(identities, AnalystEvidence)
    source_filters: list[Any] = []
    source_document_ids = {int(value) for value in _json_list(source_context.get("source_document_ids")) if str(value).isdigit()}
    source_ids = {int(value) for value in _json_list(source_context.get("source_ids")) if str(value).isdigit()}
    source_actor_ids = {int(value) for value in _json_list(source_context.get("source_actor_ids")) if str(value).isdigit()}
    source_urls = {str(value).strip().lower() for value in _json_list(source_context.get("source_urls")) if str(value).strip()}
    if document_id is not None:
        source_document_ids.add(int(document_id))
    if source_document_ids:
        source_filters.append(AnalystEvidence.source_document_id.in_(sorted(source_document_ids)))
    if source_ids:
        source_filters.append(AnalystEvidence.source_id.in_(sorted(source_ids)))
    if source_actor_ids:
        source_filters.append(AnalystEvidence.observed_source_actor_id.in_(sorted(source_actor_ids)))
        source_filters.append(AnalystEvidence.underlying_source_actor_id.in_(sorted(source_actor_ids)))
    if source_urls:
        source_filters.append(func.lower(AnalystEvidence.source_url).in_(sorted(source_urls)))
    if not filters:
        filters = source_filters
    elif source_filters:
        filters = [*filters, *source_filters]
    if not filters:
        return []
    metric_keys, periods, values = _candidate_metric_sets(metrics)
    incoming_owner_aliases = _candidate_owner_aliases(metrics)
    rows = (
        session.query(AnalystEvidence)
        .filter(AnalystEvidence.status != "archived")
        .filter(or_(*filters))
        .order_by(AnalystEvidence.updated_at.desc(), AnalystEvidence.id.desc())
        .limit(max(limit * 8, 32))
        .all()
    )

    def row_score(row: AnalystEvidence) -> int:
        payload = _json_dict(row.payload)
        forecast = _json_dict(payload.get("forecast"))
        metric = str(forecast.get("metric_key") or row.estimate_metric or "").strip().lower()
        period = str(forecast.get("period") or row.estimate_period or "").strip().lower()
        value = str(
            forecast.get("value")
            if forecast.get("value") is not None
            else forecast.get("forecast_value")
            if forecast.get("forecast_value") is not None
            else row.estimate_value
            if row.estimate_value is not None
            else forecast.get("value_text")
            or forecast.get("forecast_status")
            or ""
        ).strip().lower()
        score = 0
        if document_id is not None and row.source_document_id == int(document_id):
            score += 8
        if row.source_document_id is not None and int(row.source_document_id) in source_document_ids:
            score += 8
        if _normalize_source_url(row.source_url) in source_urls:
            score += 8
        if row.source_id is not None and int(row.source_id) in source_ids:
            score += 3
        if row.observed_source_actor_id is not None and int(row.observed_source_actor_id) in source_actor_ids:
            score += 4
        if row.underlying_source_actor_id is not None and int(row.underlying_source_actor_id) in source_actor_ids:
            score += 4
        if metric and metric in metric_keys:
            score += 4
        if period and period in periods:
            score += 2
        if value and value in values:
            score += 2
        row_owner_aliases = _source_owner_aliases(payload) | _source_owner_aliases(forecast) | _source_owner_aliases(row.source_name)
        same_owner, distinct_owner = _owner_match(row_owner_aliases, incoming_owner_aliases)
        if same_owner:
            score += 3
        elif distinct_owner:
            score -= 4
        return score

    sorted_rows = sorted(rows, key=row_score, reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for row in sorted_rows:
        payload = _json_dict(row.payload)
        forecast = _json_dict(payload.get("forecast"))
        metric = str(forecast.get("metric_key") or row.estimate_metric or "").strip()
        period = str(forecast.get("period") or row.estimate_period or "").strip()
        value_number = _float_or_none(
            forecast.get("value")
            if forecast.get("value") is not None
            else forecast.get("forecast_value")
            if forecast.get("forecast_value") is not None
            else row.estimate_value
        )
        value_text = _clean(forecast.get("value_text") or forecast.get("forecast_status"))
        same_source = document_id is not None and row.source_document_id == int(document_id)
        same_source_document = same_source or (row.source_document_id is not None and int(row.source_document_id) in source_document_ids)
        same_source_url = bool(_normalize_source_url(row.source_url) and _normalize_source_url(row.source_url) in source_urls)
        same_source_id = row.source_id is not None and int(row.source_id) in source_ids
        same_source_actor = (
            (row.observed_source_actor_id is not None and int(row.observed_source_actor_id) in source_actor_ids)
            or (row.underlying_source_actor_id is not None and int(row.underlying_source_actor_id) in source_actor_ids)
        )
        metric_match = bool(metric and metric.lower() in metric_keys)
        period_match = bool(period and period.lower() in periods)
        value_match = False
        if value_number is not None:
            value_match = str(value_number).strip().lower() in values
        if value_text:
            value_match = value_match or value_text.lower() in values
        row_owner_aliases = _source_owner_aliases(payload) | _source_owner_aliases(forecast) | _source_owner_aliases(row.source_name)
        same_estimate_owner, distinct_estimate_owner = _owner_match(row_owner_aliases, incoming_owner_aliases)
        source_chain_match = same_source_document or same_source_url or same_source_actor or same_source_id
        exact_same_series = metric_match and (period_match or value_match) and (same_estimate_owner or not incoming_owner_aliases or not row_owner_aliases)
        risk = (
            "high"
            if (same_source_document and exact_same_series)
            or (same_source_url and exact_same_series)
            or (same_source_actor and exact_same_series)
            or (metric_match and period_match and value_match and not distinct_estimate_owner)
            else "medium"
            if (source_chain_match and (metric_match or period_match or value_match))
            or (metric_match and period_match)
            or (distinct_estimate_owner and metric_match and (period_match or value_match))
            else "low"
        )
        out.append(
            {
                "id": int(row.id),
                "evidence_key": row.evidence_key,
                "subject_id": int(row.subject_id) if row.subject_id is not None else None,
                "company_id": int(row.company_id) if row.company_id is not None else None,
                "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
                "source_document_id": int(row.source_document_id) if row.source_document_id is not None else None,
                "source_id": int(row.source_id) if row.source_id is not None else None,
                "evidence_kind": row.evidence_kind,
                "source_name": row.source_name,
                "estimate_owner": payload.get("estimate_owner") or payload.get("underlying_firm") or row.source_name,
                "underlying_firm": payload.get("underlying_firm"),
                "source_url": row.source_url,
                "observed_source_actor_id": int(row.observed_source_actor_id) if row.observed_source_actor_id is not None else None,
                "underlying_source_actor_id": int(row.underlying_source_actor_id) if row.underlying_source_actor_id is not None else None,
                "metric_key": metric or None,
                "period": period or None,
                "value_number": value_number,
                "value_text": value_text or None,
                "summary": _shorten(row.summary, 900),
                "confidence": row.confidence,
                "updated_at": _dt(row.updated_at),
                "match": {
                    "same_source_document": same_source_document,
                    "same_source_url": same_source_url,
                    "same_source_id": bool(same_source_id),
                    "same_source_actor": bool(same_source_actor),
                    "same_estimate_owner": same_estimate_owner,
                    "distinct_estimate_owner": distinct_estimate_owner,
                    "metric_match": metric_match,
                    "period_match": period_match,
                    "value_match": value_match,
                },
                "duplicate_risk": risk,
            }
        )
    return out


def _existing_institutional_evidence_context(
    session: Session,
    *,
    identities: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    source_context: dict[str, Any],
    document_id: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    filters = _candidate_identity_filters(identities, InstitutionalEvidenceItem)
    source_document_ids = {int(value) for value in _json_list(source_context.get("source_document_ids")) if str(value).isdigit()}
    source_ids = {int(value) for value in _json_list(source_context.get("source_ids")) if str(value).isdigit()}
    source_urls = {str(value).strip().lower() for value in _json_list(source_context.get("source_urls")) if str(value).strip()}
    if document_id is not None:
        source_document_ids.add(int(document_id))
        filters.append(InstitutionalEvidenceItem.document_id == int(document_id))
    if source_document_ids:
        filters.append(InstitutionalEvidenceItem.document_id.in_(sorted(source_document_ids)))
    if source_ids:
        filters.append(InstitutionalEvidenceItem.source_id.in_(sorted(source_ids)))
    if source_urls:
        filters.append(or_(func.lower(InstitutionalEvidenceItem.url).in_(sorted(source_urls)), func.lower(InstitutionalEvidenceItem.canonical_url).in_(sorted(source_urls))))
    if not filters:
        return []
    metric_keys, periods, values = _candidate_metric_sets(metrics)
    incoming_owner_aliases = _candidate_owner_aliases(metrics)
    rows = (
        session.query(InstitutionalEvidenceItem)
        .filter(or_(*filters))
        .order_by(InstitutionalEvidenceItem.updated_at.desc(), InstitutionalEvidenceItem.id.desc())
        .limit(max(limit * 8, 32))
        .all()
    )

    def metric_context(row: InstitutionalEvidenceItem) -> tuple[dict[str, Any], str, set[str], set[str], set[str]]:
        payload = _json_dict(row.payload)
        forecast = _json_dict(payload.get("forecast") or payload.get("projection"))
        text = " ".join(
            _clean(value).lower()
            for value in (
                row.title,
                row.evidence_kind,
                row.provenance_summary,
                payload.get("summary"),
                payload.get("metric_key"),
                payload.get("canonical_metric_key"),
                payload.get("estimate_metric"),
                payload.get("period"),
                payload.get("forecast_period"),
                payload.get("estimate_period"),
                payload.get("value_text"),
                payload.get("forecast_status"),
                forecast.get("metric_key"),
                forecast.get("period"),
                forecast.get("value_text"),
                forecast.get("forecast_status"),
            )
            if _clean(value)
        )
        row_metric_keys: set[str] = set()
        for value in (payload.get("metric_key"), payload.get("canonical_metric_key"), payload.get("estimate_metric"), forecast.get("metric_key")):
            normalized = normalize_metric_key(value) or _clean(value)
            if normalized:
                row_metric_keys.add(normalized.lower())
        row_periods = {
            _clean(value).lower()
            for value in (
                payload.get("period"),
                payload.get("forecast_period"),
                payload.get("estimate_period"),
                forecast.get("period"),
            )
            if _clean(value)
        }
        row_values: set[str] = set()

        def add_value(value: object) -> None:
            number = _float_or_none(value)
            if number is not None:
                row_values.add(str(number).strip().lower())
            text_value = _clean(value)
            if text_value:
                row_values.add(text_value.lower())

        for value in (
            payload.get("value_number"),
            payload.get("forecast_value"),
            payload.get("estimate_value"),
            payload.get("value_text"),
            payload.get("forecast_status"),
            forecast.get("value"),
            forecast.get("forecast_value"),
            forecast.get("value_text"),
            forecast.get("forecast_status"),
        ):
            add_value(value)
        return payload, text, row_metric_keys, row_periods, row_values

    def row_score(row: InstitutionalEvidenceItem) -> int:
        payload, text, row_metric_keys, row_periods, row_values = metric_context(row)
        score = 0
        if document_id is not None and row.document_id == int(document_id):
            score += 8
        if row.document_id is not None and int(row.document_id) in source_document_ids:
            score += 8
        if _normalize_source_url(row.url) in source_urls or _normalize_source_url(row.canonical_url) in source_urls:
            score += 8
        if row.source_id is not None and int(row.source_id) in source_ids:
            score += 3
        if any(metric and (metric in text or metric in row_metric_keys) for metric in metric_keys):
            score += 4
        if any(period and (period in text or period in row_periods) for period in periods):
            score += 2
        if any(value and (value in text or value in row_values) for value in values):
            score += 2
        row_owner_aliases = _source_owner_aliases(payload) | _source_owner_aliases(row.title) | _source_owner_aliases(row.provenance_summary)
        same_owner, distinct_owner = _owner_match(row_owner_aliases, incoming_owner_aliases)
        if same_owner:
            score += 3
        elif distinct_owner:
            score -= 4
        if row.site_id is not None and any(_int(identity.get("site_id")) == int(row.site_id) for identity in identities):
            score += 2
        if row.program_id is not None and any(_int(identity.get("program_id")) == int(row.program_id) for identity in identities):
            score += 2
        return score

    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=row_score, reverse=True)[:limit]:
        payload, text, row_metric_keys, row_periods, row_values = metric_context(row)
        same_source = document_id is not None and row.document_id == int(document_id)
        same_source_document = same_source or (row.document_id is not None and int(row.document_id) in source_document_ids)
        same_source_url = bool(
            (_normalize_source_url(row.url) and _normalize_source_url(row.url) in source_urls)
            or (_normalize_source_url(row.canonical_url) and _normalize_source_url(row.canonical_url) in source_urls)
        )
        same_source_id = row.source_id is not None and int(row.source_id) in source_ids
        metric_match = any(metric and (metric in text or metric in row_metric_keys) for metric in metric_keys)
        period_match = any(period and (period in text or period in row_periods) for period in periods)
        value_match = any(value and (value in text or value in row_values) for value in values)
        site_match = row.site_id is not None and any(_int(identity.get("site_id")) == int(row.site_id) for identity in identities)
        program_match = row.program_id is not None and any(_int(identity.get("program_id")) == int(row.program_id) for identity in identities)
        row_owner_aliases = _source_owner_aliases(payload) | _source_owner_aliases(row.title) | _source_owner_aliases(row.provenance_summary)
        same_estimate_owner, distinct_estimate_owner = _owner_match(row_owner_aliases, incoming_owner_aliases)
        source_chain_match = same_source_document or same_source_url or same_source_id
        exact_same_series = metric_match and (period_match or value_match) and (same_estimate_owner or not incoming_owner_aliases or not row_owner_aliases)
        risk = (
            "high"
            if (source_chain_match and exact_same_series) or (metric_match and period_match and value_match and not distinct_estimate_owner)
            else "medium"
            if (source_chain_match and (metric_match or period_match or value_match))
            or (metric_match and period_match)
            or site_match
            or program_match
            or (distinct_estimate_owner and metric_match and (period_match or value_match))
            else "low"
        )
        out.append(
            {
                "id": int(row.id),
                "evidence_key": row.evidence_key,
                "document_id": int(row.document_id) if row.document_id is not None else None,
                "source_id": int(row.source_id) if row.source_id is not None else None,
                "subject_id": int(row.subject_id) if row.subject_id is not None else None,
                "company_id": int(row.company_id) if row.company_id is not None else None,
                "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
                "site_id": int(row.site_id) if row.site_id is not None else None,
                "program_id": int(row.program_id) if row.program_id is not None else None,
                "evidence_kind": row.evidence_kind,
                "estimate_owner": payload.get("estimate_owner") or payload.get("underlying_firm") or payload.get("source_name"),
                "underlying_firm": payload.get("underlying_firm"),
                "source_name": payload.get("source_name"),
                "title": row.title,
                "provenance_summary": _shorten(row.provenance_summary, 900),
                "url": row.url,
                "confidence": row.confidence,
                "updated_at": _dt(row.updated_at),
                "match": {
                    "same_source_document": same_source_document,
                    "same_source_url": same_source_url,
                    "same_source_id": bool(same_source_id),
                    "same_estimate_owner": same_estimate_owner,
                    "distinct_estimate_owner": distinct_estimate_owner,
                    "metric_match": metric_match,
                    "period_match": period_match,
                    "value_match": value_match,
                    "site_match": bool(site_match),
                    "program_match": bool(program_match),
                },
                "duplicate_risk": risk,
            }
        )
    return out


def _existing_institutional_observation_context(
    session: Session,
    *,
    identities: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    source_context: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    filters = _candidate_identity_filters(identities, InstitutionalObservation)
    source_ids = {int(value) for value in _json_list(source_context.get("source_ids")) if str(value).isdigit()}
    if source_ids:
        filters.append(InstitutionalObservation.source_id.in_(sorted(source_ids)))
    if not filters:
        return []
    metric_keys, periods, values = _candidate_metric_sets(metrics)
    incoming_owner_aliases = _candidate_owner_aliases(metrics)
    q = (
        session.query(InstitutionalObservation)
        .filter(InstitutionalObservation.review_status != "rejected")
        .filter(or_(*filters))
    )
    if metric_keys:
        q = q.filter(func.lower(InstitutionalObservation.metric_key).in_(sorted(metric_keys)))
    rows = q.order_by(InstitutionalObservation.updated_at.desc(), InstitutionalObservation.id.desc()).limit(max(limit * 4, 24)).all()

    def serialize(row: InstitutionalObservation) -> dict[str, Any]:
        payload = _json_dict(row.payload)
        period = _clean(payload.get("period") or payload.get("forecast_period") or payload.get("period_label"))
        if not period and row.period_date is not None:
            period = row.period_date.isoformat()
        value = str(row.value_number if row.value_number is not None else row.value_text or "").strip().lower()
        metric = str(row.metric_key or "").strip()
        metric_match = bool(metric and metric.lower() in metric_keys)
        period_match = bool(period and period.lower() in periods)
        value_match = bool(value and value in values)
        same_source_id = row.source_id is not None and int(row.source_id) in source_ids
        row_owner_aliases = _source_owner_aliases(payload)
        same_estimate_owner, distinct_estimate_owner = _owner_match(row_owner_aliases, incoming_owner_aliases)
        exact_same_series = metric_match and period_match and value_match and (same_estimate_owner or not incoming_owner_aliases or not row_owner_aliases)
        risk = (
            "high"
            if exact_same_series
            else "medium"
            if (same_source_id and (metric_match or period_match or value_match))
            or (metric_match and period_match)
            or (distinct_estimate_owner and metric_match and (period_match or value_match))
            else "low"
        )
        return {
            "id": int(row.id),
            "observation_key": row.observation_key,
            "kind": row.kind,
            "source_id": int(row.source_id) if row.source_id is not None else None,
            "subject_id": int(row.subject_id) if row.subject_id is not None else None,
            "company_id": int(row.company_id) if row.company_id is not None else None,
            "issuer_id": int(row.issuer_id) if row.issuer_id is not None else None,
            "metric_key": row.metric_key,
            "period": period or None,
            "as_of_date": row.as_of_date.isoformat() if row.as_of_date else None,
            "value_number": row.value_number,
            "value_text": _shorten(row.value_text, 500),
            "unit": row.unit,
            "currency": row.currency,
            "estimate_owner": payload.get("estimate_owner"),
            "underlying_firm": payload.get("underlying_firm"),
            "source_name": payload.get("source_name"),
            "source_document_id": payload.get("source_document_id"),
            "observed_source_actor_id": payload.get("observed_source_actor_id"),
            "underlying_source_actor_id": payload.get("underlying_source_actor_id"),
            "product_family": payload.get("product_family"),
            "segment": payload.get("segment"),
            "scenario": payload.get("scenario"),
            "period_basis": payload.get("period_basis"),
            "period_year": payload.get("period_year"),
            "period_quarter": payload.get("period_quarter"),
            "period_half": payload.get("period_half"),
            "confidence": row.confidence,
            "summary": _shorten(payload.get("summary") or payload.get("source_summary") or payload.get("rationale"), 900),
            "updated_at": _dt(row.updated_at),
            "match": {
                "metric_match": metric_match,
                "period_match": period_match,
                "value_match": value_match,
                "same_source_id": bool(same_source_id),
                "same_estimate_owner": same_estimate_owner,
                "distinct_estimate_owner": distinct_estimate_owner,
            },
            "duplicate_risk": risk,
        }

    return sorted([serialize(row) for row in rows], key=lambda row: {"high": 2, "medium": 1}.get(str(row.get("duplicate_risk")), 0), reverse=True)[:limit]


def _durable_duplicate_risk(
    *,
    prior_diffs: list[dict[str, Any]],
    existing_analyst_evidence: list[dict[str, Any]],
    existing_institutional_evidence: list[dict[str, Any]],
    existing_observations: list[dict[str, Any]],
) -> dict[str, Any]:
    reasons: list[str] = []
    level = "low"
    if prior_diffs:
        level = "high"
        reasons.append("prior_write_diff_match")
    if any(row.get("duplicate_risk") == "high" for row in existing_analyst_evidence):
        level = "high"
        reasons.append("existing_analyst_evidence_same_source_or_metric_value")
        if any(_json_dict(row.get("match")).get("same_source_url") for row in existing_analyst_evidence):
            reasons.append("existing_analyst_evidence_same_source_url")
        if any(_json_dict(row.get("match")).get("same_source_actor") for row in existing_analyst_evidence):
            reasons.append("existing_analyst_evidence_same_source_actor")
    elif any(row.get("duplicate_risk") == "medium" for row in existing_analyst_evidence) and level != "high":
        level = "medium"
        reasons.append("existing_analyst_evidence_same_metric_period")
        if any(_json_dict(row.get("match")).get("distinct_estimate_owner") for row in existing_analyst_evidence):
            reasons.append("existing_analyst_evidence_distinct_estimate_owner_compare")
    if any(row.get("duplicate_risk") == "high" for row in existing_institutional_evidence):
        level = "high"
        reasons.append("existing_institutional_evidence_same_source_or_metric_value")
        if any(_json_dict(row.get("match")).get("same_source_url") for row in existing_institutional_evidence):
            reasons.append("existing_institutional_evidence_same_source_url")
    elif any(row.get("duplicate_risk") == "medium" for row in existing_institutional_evidence) and level != "high":
        level = "medium"
        reasons.append("existing_institutional_evidence_same_metric_period_or_scope")
        if any(_json_dict(row.get("match")).get("distinct_estimate_owner") for row in existing_institutional_evidence):
            reasons.append("existing_institutional_evidence_distinct_estimate_owner_compare")
    if any(row.get("duplicate_risk") == "high" for row in existing_observations):
        level = "high"
        reasons.append("existing_institutional_observation_same_metric_value")
    elif any(row.get("duplicate_risk") == "medium" for row in existing_observations) and level != "high":
        level = "medium"
        reasons.append("existing_institutional_observation_same_metric_period")
        if any(_json_dict(row.get("match")).get("distinct_estimate_owner") for row in existing_observations):
            reasons.append("existing_institutional_observation_distinct_estimate_owner_compare")
    return {"level": level, "reasons": sorted(set(reasons))}


def _durable_research_mode_contexts(
    session: Session,
    *,
    identities: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    subject_ids: list[int] = []
    for identity in identities:
        subject_id = _int(identity.get("subject_id"))
        if subject_id is not None and int(subject_id) not in subject_ids:
            subject_ids.append(int(subject_id))
        if len(subject_ids) >= max(1, min(int(limit or 2), 4)):
            break
    if not subject_ids:
        return []
    from thoth_terminal.house_view_service import build_research_mode_context

    contexts: list[dict[str, Any]] = []
    for subject_id in subject_ids:
        try:
            context = build_research_mode_context(
                session,
                subject_id=int(subject_id),
                mode="both",
                limit=max(2, min(int(limit or 4), 6)),
            )
        except Exception:
            continue
        if not context:
            continue
        contexts.append(context)
    return contexts


def _candidate_destination_docs(session: Session, payload: dict[str, Any], *, limit: int = 4) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for field in ("candidate_writes", "research_writes"):
        for row in _json_list(payload.get(field)):
            if not isinstance(row, dict):
                continue
            dtype = _clean(row.get("destination_type") or row.get("target_type"))
            raw_id = row.get("destination_id") or row.get("target_id")
            if not dtype or not str(raw_id or "").isdigit():
                continue
            key = (_destination_type(dtype), int(raw_id))
            if key in seen:
                continue
            seen.add(key)
            try:
                doc = get_research_doc(session, destination_type=key[0], destination_id=key[1])
            except Exception:
                continue
            docs.append(
                {
                    "destination_type": doc.get("destination_type"),
                    "destination_id": doc.get("destination_id"),
                    "title": doc.get("title"),
                    "version_id": doc.get("version_id"),
                    "content_excerpt": _shorten(doc.get("content_md"), 2400),
                    "updated_at": doc.get("updated_at"),
                }
            )
            if len(docs) >= limit:
                break
        if len(docs) >= limit:
            break
    return docs


def _latest_report_snapshot(session: Session, report: ResearchReport, *, match: str, source_identity: object | None = None) -> dict[str, Any]:
    latest: ResearchReportVersion | None = None
    if report.latest_published_version_id is not None:
        latest = session.get(ResearchReportVersion, int(report.latest_published_version_id))
        if latest is not None and str(latest.status or "").strip().lower() != "published":
            latest = None
    if latest is None:
        latest = (
            session.query(ResearchReportVersion)
            .filter_by(report_id=int(report.id), status="published")
            .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
            .first()
        )
    return {
        "destination_type": "research_report",
        "destination_id": int(report.id),
        "match": match,
        "source_identity": _trim_agent_object(source_identity or {}),
        "title": report.title,
        "slug": report.slug,
        "subject_key": report.subject_key,
        "subject_id": int(report.subject_id) if report.subject_id is not None else None,
        "company_id": int(report.company_id) if report.company_id is not None else None,
        "issuer_id": int(report.issuer_id) if report.issuer_id is not None else None,
        "version_id": int(latest.id) if latest is not None else None,
        "content_excerpt": _shorten(latest.content_md if latest is not None else "", 2400),
        "updated_at": _dt(report.updated_at),
    }


def _matched_identity_destination_docs(
    session: Session,
    *,
    identities: list[dict[str, Any]],
    limit: int = 4,
) -> list[dict[str, Any]]:
    filters: list[Any] = []
    identity_by_key: dict[tuple[str, int | str], dict[str, Any]] = {}

    def add_filter(field: str, value: int | str | None, identity: dict[str, Any]) -> None:
        if value is None or value == "":
            return
        key = (field, value)
        if key in identity_by_key:
            return
        identity_by_key[key] = identity
        filters.append(getattr(ResearchReport, field) == value)

    for identity in identities:
        add_filter("subject_id", _int(identity.get("subject_id")), identity)
        add_filter("company_id", _int(identity.get("company_id")), identity)
        add_filter("issuer_id", _int(identity.get("issuer_id")), identity)
        subject_key = _clean(identity.get("subject_key"))
        if subject_key:
            add_filter("subject_key", subject_key, identity)

    if not filters:
        return []

    reports = (
        session.query(ResearchReport)
        .filter(ResearchReport.status != "archived")
        .filter(or_(*filters))
        .order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
        .limit(max(limit * 4, 12))
        .all()
    )
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for report in reports:
        if int(report.id) in seen:
            continue
        seen.add(int(report.id))
        source_identity: dict[str, Any] | None = None
        for key in (
            ("subject_id", int(report.subject_id)) if report.subject_id is not None else None,
            ("company_id", int(report.company_id)) if report.company_id is not None else None,
            ("issuer_id", int(report.issuer_id)) if report.issuer_id is not None else None,
            ("subject_key", report.subject_key),
        ):
            if key is None:
                continue
            source_identity = identity_by_key.get(key)
            if source_identity:
                break
        out.append(_latest_report_snapshot(session, report, match="matched_identity", source_identity=source_identity))
        if len(out) >= limit:
            break
    return out


def _merge_destination_documents(*groups: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    for group in groups:
        for row in group:
            key = (str(row.get("destination_type") or ""), _int(row.get("destination_id")))
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
            if len(out) >= limit:
                return out
    return out


def _prior_write_diff_matches(
    session: Session,
    *,
    document_id: int | None,
    terms: list[str],
    limit: int = 8,
) -> list[dict[str, Any]]:
    candidates = (
        session.query(LocalAgentWriteDiff)
        .order_by(LocalAgentWriteDiff.created_at.desc(), LocalAgentWriteDiff.id.desc())
        .limit(100)
        .all()
    )
    needles = [term.lower() for term in terms if len(term) >= 4][:12]
    out: list[dict[str, Any]] = []
    for row in candidates:
        source_ids = [int(value) for value in _json_list(row.source_document_ids_json) if str(value).isdigit()]
        hay = " ".join(str(value or "") for value in (row.write_summary, row.diff_text, row.destination_type)).lower()
        if document_id is not None and int(document_id) in source_ids:
            matched = True
        else:
            matched = any(needle and needle in hay for needle in needles)
        if not matched:
            continue
        out.append(
            {
                "id": int(row.id),
                "destination_type": row.destination_type,
                "destination_id": int(row.destination_id) if row.destination_id is not None else None,
                "source_document_ids": source_ids,
                "write_summary": row.write_summary,
                "confidence": row.confidence,
                "diff_excerpt": _shorten(row.diff_text, 1200),
                "created_at": _dt(row.created_at),
            }
        )
        if len(out) >= limit:
            break
    return out


def durable_candidate_context(
    session: Session,
    *,
    document_id: int | None = None,
    payload: dict[str, Any] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    source = dict(payload or {})
    if document_id is not None:
        doc = session.query(Document).options(joinedload(Document.source)).filter_by(id=int(document_id)).first()
        if doc is not None:
            source.setdefault(
                "document",
                {
                    "id": int(doc.id),
                    "title": doc.title,
                    "source_name": doc.source.name if isinstance(doc.source, Source) else None,
                },
            )
    terms = _candidate_context_terms(source, limit=8)
    searches = [
        search_objects_for_agent(session, query=term, kind="all", limit=max(3, min(limit, 8)), include_neighborhoods=False)
        for term in terms[:4]
    ]
    prior_diffs = _prior_write_diff_matches(session, document_id=document_id, terms=terms, limit=limit)
    metrics = _candidate_metric_context(source, limit=12)
    source_context = _candidate_source_context(session, source, document_id=document_id)
    identities = _candidate_identity_rows(session, source, terms=terms, limit=max(4, min(limit, 8)))
    destination_docs = _merge_destination_documents(
        _candidate_destination_docs(session, source, limit=4),
        _matched_identity_destination_docs(session, identities=identities, limit=4),
        limit=4,
    )
    existing_analyst_evidence = _existing_analyst_evidence_context(
        session,
        identities=identities,
        metrics=metrics,
        source_context=source_context,
        document_id=document_id,
        limit=max(3, min(limit, 8)),
    )
    existing_institutional_evidence = _existing_institutional_evidence_context(
        session,
        identities=identities,
        metrics=metrics,
        source_context=source_context,
        document_id=document_id,
        limit=max(3, min(limit, 8)),
    )
    existing_observations = _existing_institutional_observation_context(
        session,
        identities=identities,
        metrics=metrics,
        source_context=source_context,
        limit=max(3, min(limit, 8)),
    )
    research_mode_contexts = _durable_research_mode_contexts(
        session,
        identities=identities,
        limit=max(2, min(limit, 4)),
    )
    duplicate_risk = _durable_duplicate_risk(
        prior_diffs=prior_diffs,
        existing_analyst_evidence=existing_analyst_evidence,
        existing_institutional_evidence=existing_institutional_evidence,
        existing_observations=existing_observations,
    )
    candidate_writes = _json_list(source.get("candidate_writes") or source.get("research_writes"))
    durable_routes = _json_list(source.get("durable_routes"))
    recommended_action = "inspect"
    if str(duplicate_risk.get("level") or "").lower() == "high":
        recommended_action = "hold_for_duplicate_review"
    elif not candidate_writes and not any(isinstance(row, dict) and _clean(row.get("action")).lower() == "write" for row in durable_routes):
        recommended_action = "hold_no_write_candidate"
    return {
        "document_id": int(document_id) if document_id is not None else None,
        "query_terms": terms,
        "matched_identities": identities,
        "candidate_metrics": metrics,
        "candidate_source_context": source_context,
        "object_searches": searches,
        "destination_documents": destination_docs,
        "prior_write_diffs": prior_diffs,
        "existing_analyst_evidence": existing_analyst_evidence,
        "existing_institutional_evidence": existing_institutional_evidence,
        "existing_institutional_observations": existing_observations,
        "research_mode_contexts": research_mode_contexts,
        "duplicate_risk": duplicate_risk,
        "candidate_counts": {
            "candidate_writes": len([row for row in candidate_writes if isinstance(row, dict)]),
            "durable_routes": len([row for row in durable_routes if isinstance(row, dict)]),
            "matched_identities": len(identities),
            "candidate_metrics": len(metrics),
            "destination_documents": len(destination_docs),
            "existing_analyst_evidence": len(existing_analyst_evidence),
            "existing_institutional_evidence": len(existing_institutional_evidence),
            "existing_institutional_observations": len(existing_observations),
            "research_mode_contexts": len(research_mode_contexts),
        },
        "recommended_action": recommended_action,
        "guidance": {
            "purpose": "Use before durable writes to avoid duplicates and inspect existing Thoth state.",
            "mutation": "read_only",
            "next_step": "write only after existing analyst evidence, observations, consensus/house-view context, write diffs, and destination documents support novelty or a real update/conflict.",
        },
    }


def _route_name(route: dict[str, Any]) -> str:
    return _clean(route.get("entity_name") or route.get("entity") or route.get("name") or route.get("subject"))


def _route_text_list(value: object, *, limit: int = 8) -> list[str]:
    out: list[str] = []
    for item in _json_list(value):
        cleaned = _clean(item)
        if cleaned and cleaned not in out:
            out.append(cleaned[:1000])
        if len(out) >= limit:
            break
    return out


def _routing_rows_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in _json_list(payload.get("summary_routes")):
        if not isinstance(raw, dict):
            continue
        route = dict(raw)
        route["_lane"] = "summary"
        route["_summary_relevance"] = True
        route["_durable_relevance"] = False
        rows.append(route)
    for raw in _json_list(payload.get("durable_routes")):
        if not isinstance(raw, dict):
            continue
        route = dict(raw)
        route["_lane"] = "durable"
        route["_summary_relevance"] = bool(route.get("summary_relevance", False))
        action = str(route.get("action") or route.get("durable_action") or route.get("candidate_action") or "").strip().lower()
        if action == "write":
            route["signal_extraction_original_action"] = "write"
            route["signal_extraction_action_normalized"] = "hold"
            route["action"] = "hold"
            route["durable_action"] = "hold"
            route["rationale"] = (
                str(route.get("rationale") or "").strip()
                or "Signal extraction identified a durable candidate; durable reasoning owns write decisions."
            )
        elif action in {"review", "review_for_update", "route_for_durable_review"}:
            route["signal_extraction_original_action"] = action
            route["signal_extraction_action_normalized"] = "hold"
            route["action"] = "hold"
            route["durable_action"] = "hold"
            route["rationale"] = (
                str(route.get("rationale") or "").strip()
                or "Signal extraction routed this fact for the later durable review/write pass."
            )
        route["_durable_relevance"] = str(route.get("action") or route.get("durable_action") or route.get("candidate_action") or "").lower() in {"hold", "quarantine", "review", "review_for_update", "route_for_durable_review"}
        rows.append(route)
    for raw in _json_list(payload.get("entity_routing")):
        if not isinstance(raw, dict):
            continue
        route = dict(raw)
        lane = str(route.get("lane") or "summary").strip().lower()
        route["_lane"] = "durable" if lane == "durable" else "summary"
        route["_summary_relevance"] = bool(route.get("summary_relevance", route["_lane"] == "summary"))
        route["_durable_relevance"] = bool(route.get("durable_relevance", route["_lane"] == "durable"))
        rows.append(route)
    if not rows:
        for raw in _json_list(payload.get("entity_refs")):
            if not isinstance(raw, dict):
                continue
            route = dict(raw)
            route["_lane"] = "summary"
            route["_summary_relevance"] = True
            route["_durable_relevance"] = False
            route.setdefault("role", "mentioned")
            route.setdefault("rationale", "Legacy entity_ref; usable for daily-summary context only.")
            rows.append(route)
    for raw in _json_list(payload.get("research_writes")):
        if not isinstance(raw, dict):
            continue
        route = dict(raw)
        route["_lane"] = "durable"
        route["_summary_relevance"] = bool(route.get("summary_relevance", False))
        route["_durable_relevance"] = True
        original_action = str(route.get("action") or route.get("durable_action") or "write").strip().lower()
        if original_action:
            route["decision_review_original_action"] = original_action
        route["action"] = "hold"
        route["durable_action"] = "hold"
        route.setdefault(
            "rationale",
            "Decision review identified a write candidate; durable application owns actual write state and audit diffs.",
        )
        route.setdefault("role", "research_subject")
        rows.append(route)
    return rows


def _replace_compression_routes(
    session: Session,
    *,
    compression: LocalAgentDocumentCompression,
    payload: dict[str, Any],
    run_id: int | None,
) -> list[LocalAgentEntityRoute]:
    session.query(LocalAgentEntityRoute).filter_by(compression_id=int(compression.id)).delete(synchronize_session=False)
    now = utcnow()
    created: list[LocalAgentEntityRoute] = []
    seen: set[tuple[str, str, str]] = set()
    for route in _routing_rows_from_payload(payload):
        name = _route_name(route)
        if not name:
            continue
        lane = str(route.get("_lane") or "summary").strip().lower()[:32] or "summary"
        role = str(route.get("role") or route.get("entity_role") or "").strip().lower().replace(" ", "_")[:64] or None
        key = (lane, name.lower(), role or "")
        if key in seen:
            continue
        seen.add(key)
        conf = _float_or_none(route.get("confidence"))
        summary_conf = _float_or_none(route.get("summary_confidence")) or (conf if lane == "summary" else None)
        durable_conf = _float_or_none(route.get("durable_confidence")) or (conf if lane == "durable" else None)
        action = str(route.get("durable_action") or route.get("action") or ("hold" if lane == "durable" else "")).strip().lower()[:32] or None
        row = LocalAgentEntityRoute(
            run_id=int(run_id) if run_id is not None else int(compression.run_id) if compression.run_id is not None else None,
            compression_id=int(compression.id),
            document_id=int(compression.document_id),
            lane=lane,
            entity_name=name[:255],
            entity_type=str(route.get("entity_type") or "unknown").strip().lower()[:64] or "unknown",
            entity_role=role,
            summary_relevance=bool(route.get("_summary_relevance")),
            summary_confidence=summary_conf,
            durable_relevance=bool(route.get("_durable_relevance")),
            durable_confidence=durable_conf,
            durable_action=action,
            materiality=str(route.get("materiality") or "").strip().lower()[:32] or None,
            novelty=str(route.get("novelty") or "").strip().lower()[:32] or None,
            temporal_posture=str(route.get("temporal_posture") or route.get("time_posture") or "").strip().lower()[:32] or None,
            evidence_tier=str(route.get("evidence_tier") or route.get("source_tier") or "").strip().lower()[:32] or None,
            resolution_status=str(route.get("resolution_status") or route.get("resolution") or "").strip().lower()[:32] or None,
            new_information=str(route.get("new_information") or route.get("new_info") or "").strip() or None,
            rationale=str(route.get("rationale") or route.get("routing_reason") or "").strip() or None,
            claim_refs_json=_route_text_list(route.get("claim_refs") or route.get("claims")),
            source_claim_indexes_json=_int_list(route.get("source_claim_indexes") or route.get("claim_indexes")),
            destination_type=str(route.get("destination_type") or "").strip()[:64] or None,
            destination_id=int(route.get("destination_id")) if str(route.get("destination_id") or "").isdigit() else None,
            payload={k: v for k, v in route.items() if not str(k).startswith("_")},
            created_at=now,
        )
        session.add(row)
        created.append(row)
    session.flush()
    return created


def create_or_update_compression(
    session: Session,
    *,
    document_id: int,
    payload: dict[str, Any],
    run_id: int | None = None,
) -> LocalAgentDocumentCompression:
    now = utcnow()
    model = str(payload.get("model") or getattr(settings, "agent_default_model", "") or "").strip() or "local-agent"
    prompt_version = str(payload.get("prompt_version") or getattr(settings, "agent_prompt_version", "local-analyst-v3-staged-signal-context-search") or "").strip()
    information_gain = str(payload.get("information_gain") or "").strip().lower()[:32] or None
    document_disposition = str(payload.get("document_disposition") or payload.get("disposition") or "").strip().lower()[:32] or None
    skip_reason = str(payload.get("skip_reason") or payload.get("information_gain_reason") or "").strip() or None
    status_value = str(payload.get("status") or "").strip().lower()[:16]
    if not status_value:
        status_value = "skipped" if document_disposition in {"skip", "noop"} or information_gain == "none" else "compressed"
    row = (
        session.query(LocalAgentDocumentCompression)
        .filter(
            LocalAgentDocumentCompression.document_id == int(document_id),
            LocalAgentDocumentCompression.model == model,
            LocalAgentDocumentCompression.prompt_version == prompt_version,
        )
        .first()
    )
    if row is None:
        row = LocalAgentDocumentCompression(
            run_id=int(run_id) if run_id is not None else None,
            document_id=int(document_id),
            model=model,
            prompt_version=prompt_version,
            summary_short=str(payload.get("summary_short") or "").strip(),
            summary_detail=str(payload.get("summary_detail") or "").strip() or None,
            atomic_claims_json=[dict(v) for v in _json_list(payload.get("atomic_claims")) if isinstance(v, dict)],
            entity_refs_json=[dict(v) for v in _json_list(payload.get("entity_refs")) if isinstance(v, dict)],
            supply_chain_refs_json=[dict(v) for v in _json_list(payload.get("supply_chain_refs")) if isinstance(v, dict)],
            technology_refs_json=[dict(v) for v in _json_list(payload.get("technology_refs")) if isinstance(v, dict)],
            unresolved_entities_json=[dict(v) for v in _json_list(payload.get("unresolved_entities")) if isinstance(v, dict)],
            confidence=_float_or_none(payload.get("confidence")),
            information_gain=information_gain,
            document_disposition=document_disposition,
            skip_reason=skip_reason,
            status=status_value,
            error=str(payload.get("error")).strip() if payload.get("error") else None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.run_id = int(run_id) if run_id is not None else row.run_id
        row.summary_short = str(payload.get("summary_short") or row.summary_short or "").strip()
        row.summary_detail = str(payload.get("summary_detail") or "").strip() or row.summary_detail
        row.atomic_claims_json = [dict(v) for v in _json_list(payload.get("atomic_claims")) if isinstance(v, dict)]
        row.entity_refs_json = [dict(v) for v in _json_list(payload.get("entity_refs")) if isinstance(v, dict)]
        row.supply_chain_refs_json = [dict(v) for v in _json_list(payload.get("supply_chain_refs")) if isinstance(v, dict)]
        row.technology_refs_json = [dict(v) for v in _json_list(payload.get("technology_refs")) if isinstance(v, dict)]
        row.unresolved_entities_json = [dict(v) for v in _json_list(payload.get("unresolved_entities")) if isinstance(v, dict)]
        row.confidence = _float_or_none(payload.get("confidence")) if payload.get("confidence") is not None else row.confidence
        row.information_gain = information_gain or row.information_gain
        row.document_disposition = document_disposition or row.document_disposition
        row.skip_reason = skip_reason or row.skip_reason
        row.status = status_value or row.status or "compressed"
        row.error = str(payload.get("error")).strip() if payload.get("error") else None
        row.updated_at = now
    session.flush()
    _replace_compression_routes(session, compression=row, payload=payload, run_id=run_id)
    _upsert_document_stage_state(
        session,
        document_id=int(document_id),
        stage="signal_extraction",
        status="success" if row.status in {"compressed", "attached", "written", "skipped", "noop"} and not row.error else "error",
        run_id=run_id,
        prompt_version=prompt_version,
        model_version=model,
        failure_delta=1 if row.status == "error" or row.error else 0,
        terminal_skip_reason=skip_reason if row.status in {"skipped", "noop"} else None,
        payload={
            "compression_id": int(row.id),
            "status": row.status,
            "information_gain": information_gain,
            "document_disposition": document_disposition,
        },
    )
    return row


def _trace_rows(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in _json_list(value) if isinstance(item, dict)]


def create_or_update_deep_analysis_trace(
    session: Session,
    *,
    compression_id: int,
    payload: dict[str, Any],
    run_id: int | None = None,
) -> LocalAgentDeepAnalysisTrace:
    compression = session.query(LocalAgentDocumentCompression).filter_by(id=int(compression_id)).first()
    if compression is None:
        raise ValueError("Local-agent compression not found.")
    now = utcnow()
    model = str(payload.get("model") or compression.model or getattr(settings, "agent_default_model", "") or "").strip() or "local-agent"
    prompt_version = str(payload.get("prompt_version") or compression.prompt_version or getattr(settings, "agent_prompt_version", "local-analyst-v3-staged-signal-context-search") or "").strip()
    analysis_mode = str(payload.get("analysis_mode") or payload.get("trace_kind") or "idle_deep").strip().lower()[:32] or "idle_deep"
    status_value = str(payload.get("status") or ("error" if payload.get("error") else "success")).strip().lower()[:16] or "success"
    summary_md = str(payload.get("summary_md") or payload.get("summary") or "").strip()
    row = (
        session.query(LocalAgentDeepAnalysisTrace)
        .filter(
            LocalAgentDeepAnalysisTrace.compression_id == int(compression_id),
            LocalAgentDeepAnalysisTrace.model == model,
            LocalAgentDeepAnalysisTrace.prompt_version == prompt_version,
        )
        .first()
    )
    attrs = {
        "run_id": int(run_id) if run_id is not None else None,
        "compression_id": int(compression_id),
        "document_id": int(compression.document_id),
        "model": model,
        "prompt_version": prompt_version,
        "analysis_mode": analysis_mode,
        "status": status_value,
        "signal_level": str(payload.get("signal_level") or payload.get("signal") or "").strip().lower()[:32] or None,
        "signal_score": _float_or_none(payload.get("signal_score")),
        "summary_md": summary_md,
        "key_findings_json": _trace_rows(payload.get("key_findings")),
        "knowledge_connections_json": _trace_rows(payload.get("knowledge_connections")),
        "contradictions_json": _trace_rows(payload.get("contradictions")),
        "open_questions_json": _trace_rows(payload.get("open_questions")),
        "followups_json": _trace_rows(payload.get("followups") or payload.get("recommended_followups")),
        "payload": dict(payload or {}),
        "error": str(payload.get("error")).strip() if payload.get("error") else None,
        "updated_at": now,
    }
    if row is None:
        row = LocalAgentDeepAnalysisTrace(created_at=now, **attrs)
        session.add(row)
    else:
        for key, value in attrs.items():
            if key == "run_id" and value is None:
                continue
            setattr(row, key, value)
    session.flush()
    _upsert_document_stage_state(
        session,
        document_id=int(compression.document_id),
        stage="durable_reasoning",
        status="success" if status_value in {"success", "skipped", "noop"} else "error",
        run_id=run_id,
        prompt_version=prompt_version,
        model_version=model,
        failure_delta=1 if status_value == "error" else 0,
        terminal_skip_reason=str(payload.get("skip_reason") or "").strip() or None,
        payload={
            "compression_id": int(compression_id),
            "deep_analysis_trace_id": int(row.id),
            "analysis_mode": analysis_mode,
            "status": status_value,
            "signal_level": attrs.get("signal_level"),
        },
    )
    return row


def _deep_analysis_candidate_score(row: LocalAgentDocumentCompression) -> tuple[int, datetime, int]:
    status = str(row.status or "").strip().lower()
    disposition = str(row.document_disposition or "").strip().lower()
    gain = str(row.information_gain or "").strip().lower()
    score = 0
    if status == "error" or row.error:
        score = 1
    if gain == "medium":
        score = max(score, 2)
    if disposition in {"summary_only", "summary_candidate"}:
        score = max(score, 2)
    if disposition == "hold_for_corroboration":
        score = max(score, 3)
    if gain == "high":
        score = max(score, 4)
    if disposition == "durable_candidate":
        score = max(score, 5)
    return (score, row.created_at, int(row.id))


def list_deep_analysis_candidates(
    session: Session,
    *,
    hours: int = 48,
    limit: int = 3,
    max_text_chars: int = 12000,
) -> list[dict[str, Any]]:
    bounded_hours = max(1, min(int(hours or 48), 720))
    bounded_limit = _limit(limit, default=3, high=20)
    cutoff = utcnow() - timedelta(hours=bounded_hours)
    traced_ids = session.query(LocalAgentDeepAnalysisTrace.compression_id).filter(
        LocalAgentDeepAnalysisTrace.status.in_(["success", "skipped"])
    )
    rows = (
        session.query(LocalAgentDocumentCompression)
        .options(
            joinedload(LocalAgentDocumentCompression.document)
            .joinedload(Document.source),
        )
        .filter(LocalAgentDocumentCompression.created_at >= cutoff)
        .filter(LocalAgentDocumentCompression.status.in_(["compressed", "attached", "written", "skipped", "noop", "error"]))
        .filter(LocalAgentDocumentCompression.document_id.in_(_agent_visible_document_ids(session)))
        .filter(~LocalAgentDocumentCompression.id.in_(traced_ids))
        .order_by(LocalAgentDocumentCompression.created_at.desc(), LocalAgentDocumentCompression.id.desc())
        .limit(max(bounded_limit * 8, 20))
        .all()
    )
    material = [row for row in rows if _deep_analysis_candidate_score(row)[0] > 0]
    ranked = sorted(material, key=_deep_analysis_candidate_score, reverse=True)[:bounded_limit]
    candidates: list[dict[str, Any]] = []
    for row in ranked:
        doc = row.document if isinstance(row.document, Document) else None
        payload = {
            "compression": serialize_compression(row),
            "signal_extraction": serialize_compression(row),
            "document": document_payload(doc, max_text_chars=max_text_chars) if doc is not None else None,
            "candidate_score": _deep_analysis_candidate_score(row)[0],
        }
        candidates.append(payload)
    return candidates


def _compression_has_fresh_cloud_promotion_review(session: Session, row: LocalAgentDocumentCompression) -> bool:
    reviewed = (
        session.query(LocalAgentAction.id)
        .filter(LocalAgentAction.action_kind == "cloud_promotion_review")
        .filter(LocalAgentAction.status == "success")
        .filter(LocalAgentAction.document_id == int(row.document_id))
        .filter(LocalAgentAction.created_at >= row.updated_at)
        .first()
    )
    return reviewed is not None


def list_durable_review_candidates(
    session: Session,
    *,
    hours: int = 72,
    limit: int = 2,
    max_text_chars: int = 12000,
    document_ids: list[int] | tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    bounded_hours = max(1, min(int(hours or 72), 720))
    bounded_limit = _limit(limit, default=2, high=12)
    cutoff = utcnow() - timedelta(hours=bounded_hours)
    target_document_ids = [int(value) for value in (document_ids or []) if str(value).isdigit()]
    route_query = (
        session.query(
            LocalAgentEntityRoute.compression_id,
            func.max(LocalAgentEntityRoute.created_at).label("latest_route_at"),
            func.count(LocalAgentEntityRoute.id).label("route_count"),
        )
        .filter(LocalAgentEntityRoute.compression_id.isnot(None))
        .filter(LocalAgentEntityRoute.lane == "durable")
        .filter(LocalAgentEntityRoute.durable_relevance.is_(True))
        .filter(LocalAgentEntityRoute.created_at >= cutoff)
        .filter(_agent_visible_document_or_null(session, LocalAgentEntityRoute.document_id))
    )
    if target_document_ids:
        route_query = route_query.filter(LocalAgentEntityRoute.document_id.in_(target_document_ids))
    route_groups = route_query.group_by(LocalAgentEntityRoute.compression_id).order_by(func.max(LocalAgentEntityRoute.created_at).desc(), func.count(LocalAgentEntityRoute.id).desc()).limit(max(bounded_limit * 10, 20)).all()
    compression_ids = [int(row[0]) for row in route_groups if row[0] is not None]
    if not compression_ids:
        return []
    compression_query = (
        session.query(LocalAgentDocumentCompression)
        .options(joinedload(LocalAgentDocumentCompression.document).joinedload(Document.source))
        .filter(LocalAgentDocumentCompression.id.in_(compression_ids))
        .filter(LocalAgentDocumentCompression.status.in_(["compressed", "attached", "written", "noop", "skipped"]))
        .filter(LocalAgentDocumentCompression.error.is_(None))
        .filter(LocalAgentDocumentCompression.document_id.in_(_agent_visible_document_ids(session)))
    )
    if target_document_ids:
        compression_query = compression_query.filter(LocalAgentDocumentCompression.document_id.in_(target_document_ids))
    rows_by_id = {int(row.id): row for row in compression_query.all()}
    candidates: list[dict[str, Any]] = []
    for compression_id in compression_ids:
        row = rows_by_id.get(compression_id)
        if row is None or _compression_has_fresh_cloud_promotion_review(session, row):
            continue
        routes = (
            session.query(LocalAgentEntityRoute)
            .filter(LocalAgentEntityRoute.compression_id == int(row.id))
            .filter(LocalAgentEntityRoute.lane == "durable")
            .filter(LocalAgentEntityRoute.durable_relevance.is_(True))
            .order_by(LocalAgentEntityRoute.id.asc())
            .limit(24)
            .all()
        )
        if not routes:
            continue
        doc = row.document if isinstance(row.document, Document) else None
        signal = serialize_compression(row)
        signal["durable_routes"] = [serialize_route(route) for route in routes]
        candidates.append(
            {
                "compression": signal,
                "signal_extraction": signal,
                "document": document_payload(doc, max_text_chars=max_text_chars) if doc is not None else None,
                "durable_routes": signal["durable_routes"],
                "candidate_score": _deep_analysis_candidate_score(row)[0],
            }
        )
        if len(candidates) >= bounded_limit:
            break
    return candidates


def create_error_compression_for_model_failure(
    session: Session,
    *,
    document_id: int | None,
    run_id: int | None = None,
    error: str | None = None,
    payload: dict[str, Any] | None = None,
) -> LocalAgentDocumentCompression | None:
    if document_id is None:
        return None
    error_text = str(error or "").strip()
    if not error_text:
        return None
    normalized = error_text.lower()
    if not any(marker in normalized for marker in MODEL_OUTPUT_PARSE_ERROR_MARKERS):
        return None
    existing_terminal = (
        session.query(LocalAgentDocumentCompression)
        .filter(LocalAgentDocumentCompression.document_id == int(document_id))
        .filter(LocalAgentDocumentCompression.status.in_(["compressed", "attached", "written", "skipped", "noop"]))
        .first()
    )
    if existing_terminal is not None:
        return None
    existing_error = (
        session.query(LocalAgentDocumentCompression)
        .filter(LocalAgentDocumentCompression.document_id == int(document_id))
        .filter(LocalAgentDocumentCompression.status == "error")
        .filter(LocalAgentDocumentCompression.skip_reason == "model_output_parse_failure")
        .order_by(LocalAgentDocumentCompression.id.desc())
        .first()
    )
    if existing_error is not None:
        return existing_error
    raw_payload = dict(payload or {})
    return create_or_update_compression(
        session,
        document_id=int(document_id),
        run_id=run_id,
        payload={
            "model": raw_payload.get("model"),
            "prompt_version": raw_payload.get("prompt_version"),
            "summary_short": "Model parse failure; retry with hardened worker or backlog mode.",
            "summary_detail": (
                "The local model returned malformed or non-JSON output before the worker could write "
                "claims or durable analyst objects. This error compression keeps the failure visible."
            ),
            "atomic_claims": [],
            "entity_refs": [],
            "supply_chain_refs": [],
            "technology_refs": [],
            "unresolved_entities": [],
            "information_gain": "none",
            "document_disposition": "skip",
            "skip_reason": "model_output_parse_failure",
            "status": "error",
            "confidence": 0.0,
            "error": error_text,
        },
    )


def create_claim(session: Session, *, payload: dict[str, Any], run_id: int | None = None) -> LocalAgentEntityClaim:
    compression_id = payload.get("compression_id")
    document_id = payload.get("document_id")
    if compression_id is not None and str(compression_id).isdigit() and document_id is None:
        compression = session.query(LocalAgentDocumentCompression).filter_by(id=int(compression_id)).first()
        if compression is not None:
            document_id = int(compression.document_id)
    row = LocalAgentEntityClaim(
        run_id=int(run_id) if run_id is not None else None,
        compression_id=int(compression_id) if compression_id is not None and str(compression_id).isdigit() else None,
        document_id=int(document_id) if document_id is not None and str(document_id).isdigit() else None,
        entity_type=str(payload.get("entity_type") or "unknown").strip()[:64],
        entity_id=int(payload.get("entity_id")) if str(payload.get("entity_id") or "").isdigit() else None,
        entity_name=str(payload.get("entity_name") or "").strip()[:255],
        destination_type=str(payload.get("destination_type") or "").strip()[:64] or None,
        destination_id=int(payload.get("destination_id")) if str(payload.get("destination_id") or "").isdigit() else None,
        claim_kind=str(payload.get("claim_kind") or "business_update").strip()[:64],
        claim_text=str(payload.get("claim_text") or "").strip(),
        confidence=float(payload.get("confidence")) if payload.get("confidence") is not None else None,
        routing_reason=str(payload.get("routing_reason") or "").strip() or None,
        payload=_json_dict(payload.get("payload")),
        created_at=utcnow(),
    )
    if not row.entity_name:
        raise ValueError("entity_name is required.")
    if not row.claim_text:
        raise ValueError("claim_text is required.")
    existing = _find_existing_claim(session, row)
    if existing is not None:
        return existing
    setattr(row, "_thoth_deduped", False)
    session.add(row)
    session.flush()
    return row


def create_unresolved_entity(session: Session, *, payload: dict[str, Any], run_id: int | None = None) -> LocalAgentUnresolvedEntity:
    now = utcnow()
    row = LocalAgentUnresolvedEntity(
        run_id=int(run_id) if run_id is not None else None,
        document_id=int(payload.get("document_id")) if str(payload.get("document_id") or "").isdigit() else None,
        name=str(payload.get("name") or "").strip()[:255],
        entity_type=str(payload.get("entity_type") or "").strip()[:64] or None,
        rationale=str(payload.get("rationale") or "").strip() or None,
        candidate_payload=_json_dict(payload.get("candidate_payload") or payload),
        status=str(payload.get("status") or "open").strip()[:16],
        confidence=float(payload.get("confidence")) if payload.get("confidence") is not None else None,
        created_at=now,
        updated_at=now,
    )
    if not row.name:
        raise ValueError("name is required.")
    session.add(row)
    session.flush()
    return row


def suggest_translation_override(session: Session, *, payload: dict[str, Any], run_id: int | None = None) -> LocalAgentTranslationOverride:
    now = utcnow()
    source_text = str(payload.get("source_text") or "").strip()
    canonical_text = str(payload.get("canonical_text") or "").strip()
    language = str(payload.get("language") or "").strip() or None
    row = (
        session.query(LocalAgentTranslationOverride)
        .filter(
            LocalAgentTranslationOverride.source_text == source_text,
            LocalAgentTranslationOverride.canonical_text == canonical_text,
            LocalAgentTranslationOverride.language == language,
        )
        .first()
    )
    if row is None:
        row = LocalAgentTranslationOverride(
            run_id=int(run_id) if run_id is not None else None,
            source_text=source_text[:512],
            canonical_text=canonical_text[:512],
            language=language,
            entity_type=str(payload.get("entity_type") or "").strip()[:64] or None,
            aliases_json=[str(v).strip() for v in _json_list(payload.get("aliases")) if str(v).strip()],
            notes=str(payload.get("notes") or "").strip() or None,
            confidence=float(payload.get("confidence")) if payload.get("confidence") is not None else None,
            first_seen_document_id=int(payload.get("first_seen_document_id")) if str(payload.get("first_seen_document_id") or "").isdigit() else None,
            approved=bool(payload.get("approved", False)),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.run_id = int(run_id) if run_id is not None else row.run_id
        row.entity_type = str(payload.get("entity_type") or row.entity_type or "").strip()[:64] or None
        row.aliases_json = [str(v).strip() for v in _json_list(payload.get("aliases")) if str(v).strip()] or list(row.aliases_json or [])
        row.notes = str(payload.get("notes") or row.notes or "").strip() or None
        row.confidence = float(payload.get("confidence")) if payload.get("confidence") is not None else row.confidence
        row.approved = bool(payload.get("approved", row.approved))
        row.updated_at = now
    if not source_text or not canonical_text:
        raise ValueError("source_text and canonical_text are required.")
    session.flush()
    return row


def list_translation_overrides(session: Session, *, query: str | None = None, approved: bool | None = None, limit: int = 50) -> list[dict[str, Any]]:
    q = session.query(LocalAgentTranslationOverride)
    text = _clean(query)
    if text:
        pattern = f"%{text}%"
        q = q.filter(or_(LocalAgentTranslationOverride.source_text.ilike(pattern), LocalAgentTranslationOverride.canonical_text.ilike(pattern)))
    if approved is not None:
        q = q.filter(LocalAgentTranslationOverride.approved.is_(bool(approved)))
    rows = q.order_by(LocalAgentTranslationOverride.updated_at.desc(), LocalAgentTranslationOverride.id.desc()).limit(_limit(limit, default=50, high=200)).all()
    return [
        {
            "id": int(row.id),
            "source_text": row.source_text,
            "canonical_text": row.canonical_text,
            "language": row.language,
            "entity_type": row.entity_type,
            "aliases": list(row.aliases_json or []),
            "notes": row.notes,
            "confidence": float(row.confidence) if row.confidence is not None else None,
            "first_seen_document_id": row.first_seen_document_id,
            "approved": bool(row.approved),
            "updated_at": _dt(row.updated_at),
        }
        for row in rows
    ]


def _append_or_replace(before: str, *, content_md: str | None, append_md: str | None, write_summary: str | None) -> str:
    if content_md is not None:
        return str(content_md)
    addition = str(append_md or "").strip()
    if not addition:
        raise ValueError("content_md or append_md is required.")
    stamp = utcnow().strftime("%Y-%m-%d")
    heading = f"## Agent Update — {stamp}"
    if write_summary:
        heading = f"{heading}: {_clean(write_summary)[:80]}"
    return f"{before.rstrip()}\n\n{heading}\n\n{addition}\n".lstrip()


def _direct_write_report_payload(
    *,
    subject: ReportSubject,
    payload: dict[str, Any],
    previous_version_id: int | None,
    previous_content_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_document_ids = _int_list(payload.get("source_document_ids"))
    write_summary = _clean(payload.get("write_summary"))
    new_information = _clean(payload.get("new_information"))
    routing = _json_dict(payload.get("routing"))
    materiality = _clean(payload.get("materiality") or routing.get("materiality"))
    novelty = _clean(payload.get("novelty") or routing.get("novelty"))
    temporal_posture = _clean(payload.get("temporal_posture") or routing.get("temporal_posture"))
    information_gain = _clean(payload.get("information_gain") or routing.get("information_gain"))
    evidence_tier = _clean(payload.get("evidence_tier") or routing.get("evidence_tier"))
    confidence = _float_or_none(payload.get("confidence"))
    summary = new_information or write_summary or _clean(payload.get("append_md") or payload.get("content_md"))[:240]
    fact = {
        "fact_kind": "agent_update",
        "summary": summary,
        "write_summary": write_summary or None,
        "source_document_ids": source_document_ids,
        "confidence": confidence,
        "materiality": materiality or None,
        "novelty": novelty or None,
        "temporal_posture": temporal_posture or None,
        "information_gain": information_gain or None,
        "evidence_tier": evidence_tier or None,
    }
    agent_update = {
        "write_summary": write_summary or None,
        "new_information": new_information or None,
        "append_md": str(payload.get("append_md") or "").strip() or None,
        "source_document_ids": source_document_ids,
        "confidence": confidence,
    }
    if previous_content_json and report_payload_has_detail_sections(previous_content_json):
        merged = dict(previous_content_json)
        merged["agent_direct_write"] = True
        merged["previous_version_id"] = previous_version_id
        updates = list(merged.get("agent_direct_writes") or [])
        updates.append(agent_update)
        merged["agent_direct_writes"] = updates
        coverage_panel = dict(merged.get("coverage_panel") or {})
        coverage_panel["latest_agent_update"] = fact
        coverage_panel["needs_structuring_review"] = True
        merged["coverage_panel"] = coverage_panel
        return merged
    return {
        "agent_direct_write": True,
        "previous_version_id": previous_version_id,
        "subject": {
            "display_name": subject.display_name,
            "kind": subject.kind,
            "slug": subject.slug,
            "subject_key": subject.subject_key,
            "primary_ticker": subject.primary_ticker,
            "tickers": list(subject.tickers),
            "aliases": list(subject.aliases),
        },
        "identity_freshness": {
            "generated_at": _dt(utcnow()),
            "source": "local_agent_direct_write",
            "needs_review": True,
        },
        "coverage_panel": {
            "source_document_ids": source_document_ids,
            "confidence": confidence,
            "materiality": materiality or None,
            "novelty": novelty or None,
            "temporal_posture": temporal_posture or None,
            "information_gain": information_gain or None,
            "evidence_tier": evidence_tier or None,
            "needs_structuring_review": True,
        },
        "subject_facts": [fact],
        "agent_update": agent_update,
    }


def _latest_report_version(session: Session, report_id: int) -> ResearchReportVersion | None:
    return (
        session.query(ResearchReportVersion)
        .filter(ResearchReportVersion.report_id == int(report_id), ResearchReportVersion.status == "published")
        .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
        .first()
    )


def _write_text_signature(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def _substantive_text_duplicate(existing_text: object, candidate_text: object) -> bool:
    existing = _write_text_signature(existing_text)
    candidate = _write_text_signature(candidate_text)
    if len(candidate) < 48 or not existing:
        return False
    if candidate in existing:
        return True
    candidate_tokens = {token for token in candidate.split() if len(token) >= 4}
    if len(candidate_tokens) < 8:
        return False
    existing_tokens = {token for token in existing.split() if len(token) >= 4}
    overlap = len(candidate_tokens & existing_tokens) / max(1, len(candidate_tokens))
    return overlap >= 0.88


def _report_write_duplicate_guard(
    session: Session,
    *,
    report: ResearchReport,
    latest: ResearchReportVersion | None,
    payload: dict[str, Any],
) -> None:
    source_document_ids = set(_int_list(payload.get("source_document_ids")))
    candidate_texts = [
        payload.get("new_information"),
        payload.get("write_summary"),
        payload.get("append_md"),
        payload.get("content_md"),
    ]
    candidate_texts = [text for text in candidate_texts if _clean(text)]
    if not candidate_texts:
        return
    if latest is not None:
        existing_blob = "\n".join(
            [
                str(latest.content_md or ""),
                json.dumps(latest.content_json or {}, ensure_ascii=False, default=str),
            ]
        )
        for candidate in candidate_texts:
            if _substantive_text_duplicate(existing_blob, candidate):
                raise ValueError("Duplicate research_report write rejected: destination report already contains this substantive update.")
    recent = (
        session.query(LocalAgentWriteDiff)
        .filter(LocalAgentWriteDiff.destination_type == "research_report", LocalAgentWriteDiff.destination_id == int(report.id))
        .order_by(LocalAgentWriteDiff.created_at.desc(), LocalAgentWriteDiff.id.desc())
        .limit(25)
        .all()
    )
    for row in recent:
        prior_doc_ids = {int(value) for value in _json_list(row.source_document_ids_json) if str(value).isdigit()}
        text_blob = "\n".join(str(value or "") for value in (row.write_summary, row.diff_text, row.after_text))
        same_source_docs = bool(source_document_ids and prior_doc_ids and source_document_ids & prior_doc_ids)
        for candidate in candidate_texts:
            if _substantive_text_duplicate(text_blob, candidate):
                raise ValueError("Duplicate research_report write rejected: a prior local-agent write already recorded this update.")
            if same_source_docs and _substantive_text_duplicate(row.write_summary or "", candidate):
                raise ValueError("Duplicate research_report write rejected: same source document and write summary already exist.")


def _resolve_or_create_agent_subject(session: Session, subject: object):
    raw = _clean(subject)
    if not raw:
        raise ValueError("Missing research subject.")
    try:
        return resolve_report_subject(session, raw)
    except Exception:
        pass
    raw_lower = raw.lower()
    raw_upper = raw.lstrip("$").upper()
    instrument = (
        session.query(ResearchInstrument)
        .filter(
            or_(
                func.lower(ResearchInstrument.display_name) == raw_lower,
                func.lower(func.coalesce(ResearchInstrument.legal_name, "")) == raw_lower,
                func.lower(ResearchInstrument.instrument_key) == raw_lower,
                func.upper(func.coalesce(ResearchInstrument.ticker, "")) == raw_upper,
            )
        )
        .order_by(ResearchInstrument.id.asc())
        .first()
        )
    if instrument is None:
        return _create_agent_managed_subject(session, raw)
    now = utcnow()
    slug = re.sub(r"[^a-z0-9]+", "-", str(instrument.display_name or raw).strip().lower()).strip("-") or f"instrument-{instrument.id}"
    subject_key = f"instrument:{int(instrument.id)}"
    row = session.query(ResearchSubject).filter_by(subject_key=subject_key).first()
    if row is None:
        row = ResearchSubject(
            slug=slug,
            subject_key=subject_key,
            kind="company" if str(instrument.security_type or "").upper() in {"STK", "PRIVATE"} else "theme",
            display_name=instrument.display_name,
            legal_name=instrument.legal_name,
            description=None,
            status="active",
            issuer_id=instrument.issuer_id,
            company_id=instrument.company_id,
            primary_entity_id=None,
            extra={"tickers": [instrument.ticker] if instrument.ticker else [], "research_instrument_id": int(instrument.id)},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    aliases = {instrument.display_name, instrument.legal_name or "", instrument.ticker or "", "$" + instrument.ticker if instrument.ticker else ""}
    existing = {
        str(alias.alias or "").strip().lower()
        for alias in session.query(ResearchSubjectAlias).filter_by(subject_id=int(row.id)).all()
    }
    for alias in aliases:
        cleaned = _clean(alias)
        if not cleaned or cleaned.lower() in existing:
            continue
        session.add(
            ResearchSubjectAlias(
                subject_id=int(row.id),
                alias=cleaned,
                alias_type="ticker" if instrument.ticker and cleaned.upper().lstrip("$") == instrument.ticker.upper() else "name",
                created_at=now,
            )
        )
    session.flush()
    return resolve_report_subject(session, row.slug)


def _infer_agent_subject_kind(raw: str) -> str:
    text = raw.lower()
    if any(token in text for token in ("material", "photo", "optic", "fpca", "cpo", "memory", "semiconductor", "packaging", "lithography")):
        return "technology"
    if any(token in text for token in ("access", "policy", "restriction", "control", "export", "china")):
        return "theme"
    return "theme"


def _create_agent_managed_subject(session: Session, raw: str) -> ReportSubject:
    slug_base = _slugify(raw)
    subject_key = f"agent-subject:{slug_base}"
    row = session.query(ResearchSubject).filter_by(subject_key=subject_key).first()
    now = utcnow()
    if row is None:
        slug = slug_base
        suffix = 2
        while session.query(ResearchSubject).filter(ResearchSubject.slug == slug, ResearchSubject.subject_key != subject_key).first():
            suffix += 1
            slug = _slugify(f"{slug_base}-{suffix}")
        row = ResearchSubject(
            slug=slug,
            subject_key=subject_key,
            kind=_infer_agent_subject_kind(raw),
            display_name=raw,
            legal_name=None,
            description="Agent-managed research subject created from local analyst writes.",
            status="active",
            issuer_id=None,
            company_id=None,
            primary_entity_id=None,
            extra={"agent_managed": True, "created_by": "local_agent_direct_write", "aliases": [raw]},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    else:
        extra = dict(row.extra or {})
        extra["agent_managed"] = True
        aliases = list(extra.get("aliases") or [])
        if raw not in aliases:
            aliases.append(raw)
        extra["aliases"] = aliases
        row.extra = extra
        row.updated_at = now
        session.flush()
    existing_aliases = {
        str(alias.alias or "").strip().lower()
        for alias in session.query(ResearchSubjectAlias).filter_by(subject_id=int(row.id)).all()
    }
    if raw.lower() not in existing_aliases:
        session.add(ResearchSubjectAlias(subject_id=int(row.id), alias=raw, alias_type="agent_subject", created_at=now))
        session.flush()
    return resolve_report_subject(session, row.slug)


def _latest_asset_version(session: Session, asset_id: int) -> KnowledgeAssetVersion | None:
    return (
        session.query(KnowledgeAssetVersion)
        .filter(KnowledgeAssetVersion.asset_id == int(asset_id), KnowledgeAssetVersion.status == "published")
        .order_by(KnowledgeAssetVersion.version.desc(), KnowledgeAssetVersion.id.desc())
        .first()
    )


def _write_research_report(
    session: Session,
    *,
    destination: dict[str, Any],
    payload: dict[str, Any],
    run_id: int | None,
    action_id: int | None,
) -> LocalAgentWriteDiff:
    report: ResearchReport | None = None
    if str(destination.get("id") or destination.get("destination_id") or "").isdigit():
        report = session.query(ResearchReport).filter_by(id=int(destination.get("id") or destination.get("destination_id"))).first()
        if report is None:
            raise ValueError("Research report not found.")
        subject = resolve_report_subject(session, report)
    else:
        subject_key = destination.get("subject") or payload.get("subject")
        if not subject_key:
            raise ValueError("research_report writes require destination id or subject.")
        subject = _resolve_or_create_agent_subject(session, subject_key)
        report = _ensure_report_container(session, subject)
    latest = _latest_report_version(session, int(report.id))
    _report_write_duplicate_guard(session, report=report, latest=latest, payload=payload)
    before = latest.content_md if latest is not None else ""
    after = _append_or_replace(
        before,
        content_md=payload.get("content_md") if "content_md" in payload else None,
        append_md=payload.get("append_md"),
        write_summary=payload.get("write_summary"),
    )
    now = utcnow()
    next_version = int(latest.version) + 1 if latest is not None else 1
    content_json = _direct_write_report_payload(
        subject=subject,
        payload=payload,
        previous_version_id=int(latest.id) if latest else None,
        previous_content_json=(
            dict(latest.content_json or {})
            if latest is not None and isinstance(latest.content_json, dict)
            else None
        ),
    )
    version = ResearchReportVersion(
        report_id=int(report.id),
        version=next_version,
        status="published",
        trigger_kind="local_agent_direct_write",
        trigger_label=str(payload.get("write_summary") or "Local agent direct write")[:255],
        trigger_payload={
            "source_document_ids": _int_list(payload.get("source_document_ids")),
            "run_id": int(run_id) if run_id is not None else None,
            "destination": dict(destination),
        },
        change_fingerprint=None,
        content_json=content_json,
        content_md=after,
        coverage_json={},
        as_of_at=now,
        error=None,
        created_at=now,
        updated_at=now,
        published_at=now,
    )
    session.add(version)
    session.flush()
    report.latest_published_version_id = int(version.id)
    report.updated_at = now
    return _create_write_diff(
        session,
        run_id=run_id,
        action_id=action_id,
        destination_type="research_report",
        destination_id=int(report.id),
        source_document_ids=_int_list(payload.get("source_document_ids")),
        before_version_id=int(latest.id) if latest else None,
        after_version_id=int(version.id),
        before_text=before,
        after_text=after,
        write_summary=payload.get("write_summary"),
        confidence=payload.get("confidence"),
        payload={
            "report_slug": report.slug,
            "routing": _json_dict(payload.get("routing")),
            "materiality": payload.get("materiality"),
            "novelty": payload.get("novelty"),
            "temporal_posture": payload.get("temporal_posture"),
            "evidence_tier": payload.get("evidence_tier"),
            "new_information": payload.get("new_information"),
        },
    )


def _write_knowledge_asset(
    session: Session,
    *,
    destination: dict[str, Any],
    payload: dict[str, Any],
    run_id: int | None,
    action_id: int | None,
) -> LocalAgentWriteDiff:
    asset: KnowledgeAsset | None = None
    if str(destination.get("id") or destination.get("destination_id") or "").isdigit():
        asset = session.query(KnowledgeAsset).filter_by(id=int(destination.get("id") or destination.get("destination_id"))).first()
    elif destination.get("slug"):
        asset = session.query(KnowledgeAsset).filter_by(slug=str(destination.get("slug"))).first()
    if asset is None:
        raise ValueError("Knowledge asset writes currently require an existing asset id or slug.")
    latest = _latest_asset_version(session, int(asset.id))
    before = latest.content_md if latest is not None else ""
    after = _append_or_replace(
        before,
        content_md=payload.get("content_md") if "content_md" in payload else None,
        append_md=payload.get("append_md"),
        write_summary=payload.get("write_summary"),
    )
    now = utcnow()
    next_version = int(latest.version) + 1 if latest is not None else 1
    version = KnowledgeAssetVersion(
        asset_id=int(asset.id),
        version=next_version,
        status="published",
        title=str(payload.get("title") or asset.title),
        summary=str(payload.get("summary") or asset.summary or "").strip() or None,
        content_md=after,
        content_json={"agent_direct_write": True, "previous_version_id": int(latest.id) if latest else None},
        coverage_json={},
        evidence_bundle=[{"document_id": doc_id} for doc_id in _int_list(payload.get("source_document_ids"))],
        ai_metadata={"run_id": int(run_id) if run_id is not None else None, "model": payload.get("model")},
        as_of_at=now,
        created_at=now,
        updated_at=now,
        published_at=now,
    )
    session.add(version)
    session.flush()
    asset.latest_published_version_id = int(version.id)
    asset.summary = version.summary or asset.summary
    asset.updated_at = now
    return _create_write_diff(
        session,
        run_id=run_id,
        action_id=action_id,
        destination_type="knowledge_asset",
        destination_id=int(asset.id),
        source_document_ids=_int_list(payload.get("source_document_ids")),
        before_version_id=int(latest.id) if latest else None,
        after_version_id=int(version.id),
        before_text=before,
        after_text=after,
        write_summary=payload.get("write_summary"),
        confidence=payload.get("confidence"),
        payload={
            "asset_slug": asset.slug,
            "asset_kind": asset.kind,
            "routing": _json_dict(payload.get("routing")),
            "materiality": payload.get("materiality"),
            "novelty": payload.get("novelty"),
            "temporal_posture": payload.get("temporal_posture"),
            "evidence_tier": payload.get("evidence_tier"),
            "new_information": payload.get("new_information"),
        },
    )


def _write_supply_chain(
    session: Session,
    *,
    destination: dict[str, Any],
    payload: dict[str, Any],
    run_id: int | None,
    action_id: int | None,
) -> LocalAgentWriteDiff:
    chain_id = destination.get("id") or destination.get("destination_id")
    if not str(chain_id or "").isdigit():
        raise ValueError("supply_chain writes require destination id.")
    chain = session.query(SupplyChain).filter_by(id=int(chain_id)).first()
    if chain is None:
        raise ValueError("Supply chain not found.")
    data = dict(chain.data or {})
    before = str(data.get("agent_research_md") or "")
    after = _append_or_replace(
        before,
        content_md=payload.get("content_md") if "content_md" in payload else None,
        append_md=payload.get("append_md"),
        write_summary=payload.get("write_summary"),
    )
    data["agent_research_md"] = after
    data["agent_research_updated_at"] = _dt(utcnow())
    data["agent_research_source_document_ids"] = _int_list(payload.get("source_document_ids"))
    chain.data = data
    chain.updated_at = utcnow()
    return _create_write_diff(
        session,
        run_id=run_id,
        action_id=action_id,
        destination_type="supply_chain",
        destination_id=int(chain.id),
        source_document_ids=_int_list(payload.get("source_document_ids")),
        before_version_id=None,
        after_version_id=None,
        before_text=before,
        after_text=after,
        write_summary=payload.get("write_summary"),
        confidence=payload.get("confidence"),
        payload={"chain_slug": chain.slug},
    )


def _dataset_materialization_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("rows", "dataset_rows", "items", "facts"):
        rows = [dict(row) for row in _json_list(payload.get(key)) if isinstance(row, dict)]
        if rows:
            return rows
    row = _json_dict(payload.get("row"))
    if row:
        return [row]
    metric_key = _clean(payload.get("metric_key") or payload.get("metric"))
    value = payload.get("value")
    if value in (None, ""):
        value = payload.get("value_number")
    if value in (None, ""):
        value = payload.get("value_text")
    if metric_key and value not in (None, ""):
        return [
            {
                "row_key": payload.get("row_key") or payload.get("fact_key"),
                "period_date": payload.get("period_date") or payload.get("period") or payload.get("as_of_date"),
                "metric_key": metric_key,
                "value_number": value if isinstance(value, (int, float)) else None,
                "value_text": None if isinstance(value, (int, float)) else value,
                "unit": payload.get("unit"),
                "source_owner": payload.get("source_owner") or payload.get("estimate_owner"),
                "source_document_id": (_int_list(payload.get("source_document_ids")) or [None])[0],
            }
        ]
    return []


def _dataset_materialization_links(payload: dict[str, Any]) -> list[dict[str, Any]]:
    links = [
        dict(row)
        for row in _json_list(payload.get("links"))
        if isinstance(row, dict) and str(row.get("object_type") or "").strip().lower() in DATASET_OBJECT_TYPES and str(row.get("object_id") or "").isdigit()
    ]
    for key, object_type in (
        ("instrument_request_id", "instrument_request"),
        ("instrument_request_ids", "instrument_request"),
        ("supply_chain_id", "supply_chain"),
        ("supply_chain_ids", "supply_chain"),
        ("company_id", "company"),
        ("company_ids", "company"),
    ):
        for object_id in _int_list(payload.get(key)):
            links.append({"object_type": object_type, "object_id": int(object_id), "relation": "context"})
    return links


def _dataset_state_summary(session: Session, dataset: Dataset | None) -> dict[str, Any]:
    if dataset is None:
        return {}
    row_count = 0
    latest = None
    try:
        from thoth_terminal.dataset_service import latest_dataset_version

        latest = latest_dataset_version(session, int(dataset.id), status="draft") or latest_dataset_version(session, int(dataset.id), status="published")
    except Exception:
        latest = None
    if latest is not None:
        try:
            row_count = int(len(getattr(latest, "rows", []) or []))
        except Exception:
            row_count = 0
    return {
        "id": int(dataset.id),
        "slug": dataset.slug,
        "family": dataset.family,
        "title": dataset.title,
        "status": dataset.status,
        "row_count": row_count,
        "updated_at": _dt(dataset.updated_at),
    }


def _write_existing_dataset_rows(
    session: Session,
    *,
    dataset: Dataset,
    payload: dict[str, Any],
    run_id: int | None,
    action_id: int | None,
) -> LocalAgentWriteDiff:
    rows = _dataset_materialization_rows(payload)
    if not rows:
        raise ValueError("dataset writes require rows.")
    before = _dataset_state_summary(session, dataset)
    row_keys: list[str] = []
    for row in rows:
        row_key = _clean(row.get("row_key") or row.get("id"))
        if not row_key:
            raise ValueError("dataset row writes require row_key.")
        values = _json_dict(row.get("values"))
        if not values:
            values = {
                str(key): value
                for key, value in dict(row).items()
                if key not in {"row_key", "id", "sort_order", "extra", "rationale", "confidence"}
            }
        if not values:
            raise ValueError("dataset row writes require row values.")
        upsert_dataset_draft_row(
            session,
            dataset,
            row_key=row_key,
            values=values,
            sort_order=int(row.get("sort_order")) if str(row.get("sort_order") or "").isdigit() else None,
        )
        row_keys.append(row_key)
    dataset.updated_at = utcnow()
    session.add(dataset)
    session.flush()
    after = {**_dataset_state_summary(session, dataset), "row_keys": row_keys, "rows_written": len(row_keys)}
    return _create_write_diff(
        session,
        run_id=run_id,
        action_id=action_id,
        destination_type="dataset_or_data_series",
        destination_id=int(dataset.id),
        source_document_ids=_int_list(payload.get("source_document_ids")),
        before_version_id=None,
        after_version_id=None,
        before_text=json.dumps(before, sort_keys=True, default=str),
        after_text=json.dumps(after, sort_keys=True, default=str),
        write_summary=payload.get("write_summary") or payload.get("label") or payload.get("new_information"),
        confidence=payload.get("confidence"),
        payload={
            "dataset_id": int(dataset.id),
            "dataset_slug": dataset.slug,
            "family": dataset.family,
            "row_keys": row_keys,
            "routing": _json_dict(payload.get("routing")),
            "materiality": payload.get("materiality"),
            "novelty": payload.get("novelty"),
            "temporal_posture": payload.get("temporal_posture"),
            "evidence_tier": payload.get("evidence_tier"),
            "new_information": payload.get("new_information"),
        },
    )


def _write_dataset_or_data_series(
    session: Session,
    *,
    destination: dict[str, Any],
    payload: dict[str, Any],
    run_id: int | None,
    action_id: int | None,
) -> LocalAgentWriteDiff:
    family = _clean(payload.get("dataset_family") or payload.get("family") or destination.get("family"))
    dataset_id = payload.get("dataset_id") or payload.get("id") or destination.get("id") or destination.get("destination_id")
    if str(dataset_id or "").isdigit():
        dataset = session.query(Dataset).filter_by(id=int(dataset_id)).first()
        if dataset is None:
            raise ValueError("Dataset not found.")
        return _write_existing_dataset_rows(session, dataset=dataset, payload=payload, run_id=run_id, action_id=action_id)
    if not family:
        raise ValueError("dataset_or_data_series writes require dataset_family.")
    rows = _dataset_materialization_rows(payload)
    if not rows:
        raise ValueError("dataset_or_data_series writes require at least one row or metric value.")
    slug = _clean(destination.get("slug") or payload.get("slug") or payload.get("dataset_slug"))
    existing = session.query(Dataset).filter_by(slug=slug).first() if slug else None
    before = _dataset_state_summary(session, existing)
    provenance = _json_dict(payload.get("provenance"))
    source_doc_ids = _int_list(payload.get("source_document_ids"))
    if source_doc_ids:
        provenance.setdefault("source_document_id", int(source_doc_ids[0]))
    for key in ("source_owner", "estimate_owner", "evidence_tier", "evidence_tier_summary"):
        if payload.get(key) not in (None, ""):
            provenance.setdefault(key, payload.get(key))
    result = upsert_family_dataset_materialization(
        session,
        family=family,
        rows=rows,
        slug=slug or None,
        title=_clean(destination.get("title") or payload.get("title") or payload.get("dataset_title")) or None,
        description=_clean(payload.get("description") or payload.get("write_summary") or payload.get("new_information")) or None,
        category=_clean(payload.get("category") or destination.get("category")) or "structured_data",
        source_kind=_clean(payload.get("source_kind") or destination.get("source_kind")) or None,
        source_event=_clean(payload.get("source_event") or "local_agent_dataset_write") or None,
        label=_clean(payload.get("label") or "Local-agent dataset materialization") or None,
        extra={
            "local_agent_write": True,
            "run_id": int(run_id) if run_id is not None else None,
            "write_summary": _clean(payload.get("write_summary")),
            **_json_dict(payload.get("extra")),
        },
        provenance=provenance,
        links=_dataset_materialization_links(payload),
    )
    dataset = result["dataset"]
    after = {
        **_dataset_state_summary(session, dataset),
        "rows_received": result.get("rows_received"),
        "rows_written": result.get("rows_written"),
        "row_keys": list(result.get("row_keys") or []),
    }
    return _create_write_diff(
        session,
        run_id=run_id,
        action_id=action_id,
        destination_type="dataset_or_data_series",
        destination_id=int(dataset.id),
        source_document_ids=source_doc_ids,
        before_version_id=None,
        after_version_id=None,
        before_text=json.dumps(before, sort_keys=True, default=str),
        after_text=json.dumps(after, sort_keys=True, default=str),
        write_summary=payload.get("write_summary") or payload.get("new_information"),
        confidence=payload.get("confidence"),
        payload={
            "dataset_id": int(dataset.id),
            "dataset_slug": dataset.slug,
            "family": dataset.family,
            "row_keys": list(result.get("row_keys") or []),
            "routing": _json_dict(payload.get("routing")),
            "materiality": payload.get("materiality"),
            "novelty": payload.get("novelty"),
            "temporal_posture": payload.get("temporal_posture"),
            "evidence_tier": payload.get("evidence_tier"),
            "new_information": payload.get("new_information"),
        },
    )


def _binding_payload(binding: SupplyChainMetricBinding | None) -> dict[str, Any]:
    if binding is None:
        return {}
    return {
        "id": int(binding.id),
        "snapshot_id": int(binding.snapshot_id),
        "dataset_id": int(binding.dataset_id) if binding.dataset_id is not None else None,
        "dataset_view_id": int(binding.dataset_view_id) if binding.dataset_view_id is not None else None,
        "scope_type": binding.scope_type,
        "scope_key": binding.scope_key,
        "metric_kind": binding.metric_kind,
        "column_key": binding.column_key,
        "aggregation": binding.aggregation,
        "extra": dict(binding.extra or {}),
    }


def _write_supply_chain_metric_binding(
    session: Session,
    *,
    destination: dict[str, Any],
    payload: dict[str, Any],
    run_id: int | None,
    action_id: int | None,
) -> LocalAgentWriteDiff:
    chain_id = (
        destination.get("supply_chain_id")
        or destination.get("chain_id")
        or payload.get("supply_chain_id")
        or payload.get("chain_id")
        or destination.get("id")
        or destination.get("destination_id")
    )
    if not str(chain_id or "").isdigit():
        raise ValueError("supply_chain_metric_binding writes require supply_chain_id or destination id.")
    dataset_id = payload.get("dataset_id") or destination.get("dataset_id")
    if not str(dataset_id or "").isdigit():
        raise ValueError("supply_chain_metric_binding writes require dataset_id.")
    metric_kind = _clean(payload.get("metric_kind") or payload.get("metric_key") or destination.get("metric_kind"))
    if not metric_kind:
        raise ValueError("supply_chain_metric_binding writes require metric_kind.")
    chain = session.query(SupplyChain).filter_by(id=int(chain_id)).first()
    if chain is None:
        raise ValueError("Supply chain not found.")
    dataset = session.query(Dataset).filter_by(id=int(dataset_id)).first()
    if dataset is None:
        raise ValueError("Dataset not found.")
    scope_type = _clean(payload.get("scope_type") or destination.get("scope_type") or "chain") or "chain"
    scope_key = _clean(payload.get("scope_key") or destination.get("scope_key") or "chain") or "chain"
    draft = ensure_draft_snapshot(session, chain)
    existing = (
        session.query(SupplyChainMetricBinding)
        .filter_by(snapshot_id=int(draft.id), scope_type=scope_type, scope_key=scope_key, metric_kind=metric_kind)
        .first()
    )
    before = _binding_payload(existing)
    extra = {
        "local_agent_write": True,
        "run_id": int(run_id) if run_id is not None else None,
        "write_summary": _clean(payload.get("write_summary")),
        "source_document_ids": _int_list(payload.get("source_document_ids")),
        **_json_dict(payload.get("extra")),
    }
    binding = bind_supply_chain_dataset(
        session,
        chain,
        dataset_id=int(dataset_id),
        dataset_view_id=int(payload.get("dataset_view_id")) if str(payload.get("dataset_view_id") or "").isdigit() else None,
        scope_type=scope_type,
        scope_key=scope_key,
        metric_kind=metric_kind,
        column_key=_clean(payload.get("column_key") or destination.get("column_key")) or None,
        aggregation=_clean(payload.get("aggregation") or destination.get("aggregation")) or None,
        extra=extra,
    )
    chain.updated_at = utcnow()
    after = _binding_payload(binding)
    return _create_write_diff(
        session,
        run_id=run_id,
        action_id=action_id,
        destination_type="supply_chain_metric_binding",
        destination_id=int(binding.id),
        source_document_ids=_int_list(payload.get("source_document_ids")),
        before_version_id=None,
        after_version_id=None,
        before_text=json.dumps(before, sort_keys=True, default=str),
        after_text=json.dumps(after, sort_keys=True, default=str),
        write_summary=payload.get("write_summary") or payload.get("new_information"),
        confidence=payload.get("confidence"),
        payload={
            "supply_chain_id": int(chain.id),
            "supply_chain_slug": chain.slug,
            "dataset_id": int(dataset.id),
            "dataset_slug": dataset.slug,
            "metric_kind": metric_kind,
            "scope_type": scope_type,
            "scope_key": scope_key,
            "routing": _json_dict(payload.get("routing")),
            "materiality": payload.get("materiality"),
            "novelty": payload.get("novelty"),
            "temporal_posture": payload.get("temporal_posture"),
            "evidence_tier": payload.get("evidence_tier"),
            "new_information": payload.get("new_information"),
        },
    )


def _create_write_diff(
    session: Session,
    *,
    run_id: int | None,
    action_id: int | None,
    destination_type: str,
    destination_id: int | None,
    source_document_ids: list[int],
    before_version_id: int | None,
    after_version_id: int | None,
    before_text: str | None,
    after_text: str | None,
    write_summary: object | None,
    confidence: object | None,
    payload: dict[str, Any] | None = None,
) -> LocalAgentWriteDiff:
    row = LocalAgentWriteDiff(
        run_id=int(run_id) if run_id is not None else None,
        action_id=int(action_id) if action_id is not None else None,
        destination_type=destination_type,
        destination_id=int(destination_id) if destination_id is not None else None,
        source_document_ids_json=list(source_document_ids),
        before_version_id=before_version_id,
        after_version_id=after_version_id,
        before_text=before_text,
        after_text=after_text,
        diff_text=_diff(before_text, after_text, label=f"{destination_type}:{destination_id}"),
        write_summary=str(write_summary).strip() if write_summary else None,
        confidence=float(confidence) if confidence is not None else None,
        payload=dict(payload or {}),
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _validate_durable_write_payload(session: Session, payload: dict[str, Any]) -> None:
    confidence = _float_or_none(payload.get("confidence"))
    if confidence is None or not 0 <= confidence <= 1:
        raise ValueError("Durable research writes require confidence between 0 and 1.")
    routing = _json_dict(payload.get("routing"))
    materiality = str(payload.get("materiality") or routing.get("materiality") or "").strip().lower()
    novelty = str(payload.get("novelty") or routing.get("novelty") or "").strip().lower()
    temporal = str(payload.get("temporal_posture") or routing.get("temporal_posture") or "").strip().lower()
    information_gain = str(payload.get("information_gain") or routing.get("information_gain") or "").strip().lower()
    model_use = str(payload.get("model_use") or routing.get("model_use") or "").strip().lower()
    uncertainty = _clean(payload.get("uncertainty") or routing.get("uncertainty"))
    new_information = _clean(payload.get("new_information") or routing.get("new_information"))
    if materiality and materiality not in {"high", "medium"}:
        raise ValueError("Durable research writes require medium or high materiality.")
    if information_gain in {"none", "low"}:
        raise ValueError("Durable research writes require medium or high information gain.")
    if not materiality:
        raise ValueError("Durable research writes require an explicit materiality assessment.")
    if not novelty:
        raise ValueError("Durable research writes require an explicit novelty assessment.")
    if not information_gain:
        raise ValueError("Durable research writes require an explicit information_gain assessment.")
    if novelty in {"duplicate", "background"}:
        raise ValueError("Duplicate/background context must be held for summary context, not written to durable memory.")
    if temporal in {"backward_looking", "historical_context", "retrospective"} and novelty not in {"conflict", "outdated"}:
        raise ValueError("Backward-looking or historical context must be held for summary context unless it identifies a conflict or outdated memory.")
    if not temporal:
        raise ValueError("Durable research writes require an explicit temporal_posture.")
    if len(new_information) < 20:
        raise ValueError("Durable research writes require a concise statement of what is new information.")
    # Weak leading signals can change a scenario or watchpoint without being
    # promoted to a confirmed base case. Formality of the source is not a gate.
    if confidence < 0.7:
        if not (confidence >= 0.4 and temporal == "forward_looking"
                and model_use in {"scenario_input", "watch_only"} and len(uncertainty) >= 20):
            raise ValueError("Confidence below 0.70 requires a forward-looking scenario/watchpoint and explicit uncertainty (minimum 0.40).")


def write_research_destination(
    session: Session,
    *,
    destination: dict[str, Any],
    payload: dict[str, Any],
    run_id: int | None = None,
    action_id: int | None = None,
) -> LocalAgentWriteDiff:
    _validate_durable_write_payload(session, payload)
    dtype = _destination_type(destination.get("type") or destination.get("destination_type"))
    if dtype in {"company", "research_subject"}:
        # Company/research_subject writes resolve to the canonical research report.
        subject = destination.get("subject") or destination.get("slug")
        if not subject and dtype == "company" and str(destination.get("id") or destination.get("destination_id") or "").isdigit():
            company = session.query(Company).filter_by(id=int(destination.get("id") or destination.get("destination_id"))).first()
            subject = company.slug if company is not None else None
        if not subject and dtype == "research_subject" and str(destination.get("id") or destination.get("destination_id") or "").isdigit():
            subject_row = session.query(ResearchSubject).filter_by(id=int(destination.get("id") or destination.get("destination_id"))).first()
            subject = subject_row.slug if subject_row is not None else None
        subject = subject or destination.get("id") or destination.get("destination_id")
        destination = {"type": "research_report", "subject": subject}
        dtype = "research_report"
    if dtype == "research_report":
        return _write_research_report(session, destination=destination, payload=payload, run_id=run_id, action_id=action_id)
    if dtype == "knowledge_asset":
        return _write_knowledge_asset(session, destination=destination, payload=payload, run_id=run_id, action_id=action_id)
    if dtype == "supply_chain":
        return _write_supply_chain(session, destination=destination, payload=payload, run_id=run_id, action_id=action_id)
    if dtype == "dataset_or_data_series":
        return _write_dataset_or_data_series(session, destination=destination, payload=payload, run_id=run_id, action_id=action_id)
    if dtype == "supply_chain_metric_binding":
        return _write_supply_chain_metric_binding(session, destination=destination, payload=payload, run_id=run_id, action_id=action_id)
    raise ValueError(f"Unsupported destination type: {dtype}")


def upsert_daily_summary(session: Session, *, payload: dict[str, Any], run_id: int | None = None) -> LocalAgentDailySummary:
    raw_date = str(payload.get("summary_date") or date.today().isoformat())
    summary_date = date.fromisoformat(raw_date[:10])
    kind = str(payload.get("summary_kind") or "agent_dispatch").strip()[:64]
    now = utcnow()
    row = (
        session.query(LocalAgentDailySummary)
        .filter(LocalAgentDailySummary.summary_date == summary_date, LocalAgentDailySummary.summary_kind == kind)
        .first()
    )
    if row is None:
        row = LocalAgentDailySummary(
            run_id=int(run_id) if run_id is not None else None,
            summary_date=summary_date,
            summary_kind=kind,
            title=str(payload.get("title") or f"Agent Dispatch: Saved Work Product - {summary_date.isoformat()}").strip()[:255],
            content_md=str(payload.get("content_md") or ""),
            source_document_ids_json=_int_list(payload.get("source_document_ids")),
            compression_ids_json=_int_list(payload.get("compression_ids")),
            payload=_json_dict(payload.get("payload")),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.run_id = int(run_id) if run_id is not None else row.run_id
        row.title = str(payload.get("title") or row.title).strip()[:255]
        row.content_md = str(payload.get("content_md") or row.content_md)
        row.source_document_ids_json = _int_list(payload.get("source_document_ids")) or list(row.source_document_ids_json or [])
        row.compression_ids_json = _int_list(payload.get("compression_ids")) or list(row.compression_ids_json or [])
        row.payload = _json_dict(payload.get("payload")) or dict(row.payload or {})
        row.updated_at = now
    session.flush()
    return row


def _agent_document_display_kind(*, kind: str, title: object | None = None) -> str:
    normalized = str(kind or "").strip().lower().replace("-", "_")
    title_text = str(title or "").strip().lower()
    if normalized in {"daily_summary", "global"} or "daily summary" in title_text:
        return "Scheduled daily dispatch"
    if normalized in {"agent_dispatch", "dispatch"}:
        return "Analyst dispatch"
    return normalized.replace("_", " ").title() if normalized else "Analyst dispatch"


def _agent_document_display_title(*, kind: str, title: object | None = None, summary_date: date | None = None) -> str:
    raw = str(title or "").strip()
    normalized = str(kind or "").strip().lower().replace("-", "_")
    if normalized in {"daily_summary", "global"} or "daily summary" in raw.lower():
        suffix = f" - {summary_date.isoformat()}" if summary_date else ""
        return f"Scheduled Daily Dispatch{suffix}"
    return raw or "Analyst Dispatch"


def _agent_document_label(doc: Document | None) -> str:
    if doc is None:
        return "Unknown document"
    return str(doc.title or doc.url or f"Document {int(doc.id)}").strip()


def _serialize_agent_read(row: LocalAgentDocumentRead) -> dict[str, Any]:
    doc = row.document if isinstance(row.document, Document) else None
    source = doc.source if isinstance(doc, Document) and isinstance(doc.source, Source) else None
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "document_id": int(row.document_id),
        "document_title": _agent_document_label(doc),
        "document_href": f"/docs/{int(row.document_id)}",
        "source_name": source.name if source else None,
        "read_kind": row.read_kind,
        "triage_state": doc.triage_state if doc is not None else None,
        "created_at": _dt(row.created_at),
    }


def _serialize_agent_compression_for_workspace(row: LocalAgentDocumentCompression) -> dict[str, Any]:
    doc = row.document if isinstance(row.document, Document) else None
    source = doc.source if isinstance(doc, Document) and isinstance(doc.source, Source) else None
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "document_id": int(row.document_id),
        "document_title": _agent_document_label(doc),
        "document_href": f"/docs/{int(row.document_id)}",
        "source_name": source.name if source else None,
        "published_at": _dt(doc.published_at) if doc is not None else None,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
        "status": row.status,
        "summary_short": row.summary_short,
        "summary_detail": row.summary_detail,
        "information_gain": row.information_gain,
        "document_disposition": row.document_disposition,
        "skip_reason": row.skip_reason,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "atomic_claims": list(row.atomic_claims_json or []),
        "entity_refs": [],
        "unresolved_entities": [],
        "error": _shorten(row.error, 500),
    }


def _serialize_deep_trace_for_workspace(row: LocalAgentDeepAnalysisTrace) -> dict[str, Any]:
    compression = row.compression if isinstance(row.compression, LocalAgentDocumentCompression) else None
    doc = row.document if isinstance(row.document, Document) else (compression.document if isinstance(compression, LocalAgentDocumentCompression) else None)
    source = doc.source if isinstance(doc, Document) and isinstance(doc.source, Source) else None
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "compression_id": int(row.compression_id),
        "document_id": int(row.document_id),
        "document_title": _agent_document_label(doc),
        "document_href": f"/docs/{int(row.document_id)}",
        "source_name": source.name if source else None,
        "analysis_mode": row.analysis_mode,
        "status": row.status,
        "signal_level": row.signal_level,
        "signal_score": float(row.signal_score) if row.signal_score is not None else None,
        "summary_md": row.summary_md,
        "summary": _shorten(row.summary_md, 800),
        "key_findings": list(row.key_findings_json or [])[:5],
        "knowledge_connections": list(row.knowledge_connections_json or [])[:5],
        "contradictions": list(row.contradictions_json or [])[:5],
        "open_questions": list(row.open_questions_json or [])[:5],
        "followups": list(row.followups_json or [])[:5],
        "error": _shorten(row.error, 500),
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _workspace_comp_materiality(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "").strip().lower()
    disposition = str(item.get("document_disposition") or "").strip().lower()
    gain = str(item.get("information_gain") or "").strip().lower()
    if status == "error" or item.get("error"):
        return "blocked"
    if disposition == "durable_candidate":
        return "durable candidate"
    if disposition == "hold_for_corroboration":
        return "needs corroboration"
    if gain in {"high", "medium"}:
        return f"{gain} signal"
    if disposition in {"summary_only", "summary_candidate"}:
        return "summary context"
    if gain == "low":
        return "low signal"
    return "routine"


def _workspace_comp_is_material(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").strip().lower()
    disposition = str(item.get("document_disposition") or "").strip().lower()
    gain = str(item.get("information_gain") or "").strip().lower()
    return bool(
        status == "error"
        or item.get("error")
        or disposition in {"durable_candidate", "hold_for_corroboration", "summary_only", "summary_candidate"}
        or gain in {"high", "medium"}
    )


def _workspace_comp_needs_review(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").strip().lower()
    disposition = str(item.get("document_disposition") or "").strip().lower()
    gain = str(item.get("information_gain") or "").strip().lower()
    return bool(
        status == "error"
        or item.get("error")
        or disposition in {"durable_candidate", "hold_for_corroboration"}
        or gain == "high"
    )


def _workspace_comp_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    disposition = str(item.get("document_disposition") or "").strip().lower()
    gain = str(item.get("information_gain") or "").strip().lower()
    status = str(item.get("status") or "").strip().lower()
    rank = 0
    if status == "error" or item.get("error"):
        rank = 5
    elif disposition == "durable_candidate":
        rank = 4
    elif disposition == "hold_for_corroboration":
        rank = 3
    elif gain == "high":
        rank = 2
    elif gain == "medium":
        rank = 1
    return (rank, str(item.get("created_at") or ""))


def _workspace_destination_label(row: LocalAgentWriteDiff) -> str:
    dest = str(row.destination_type or "object").replace("_", " ")
    if row.destination_id is None:
        return dest
    return f"{dest} #{int(row.destination_id)}"


def _serialize_workspace_write(row: LocalAgentWriteDiff) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "action_id": int(row.action_id) if row.action_id is not None else None,
        "destination_type": row.destination_type,
        "destination_id": int(row.destination_id) if row.destination_id is not None else None,
        "destination_label": _workspace_destination_label(row),
        "source_document_ids": list(row.source_document_ids_json or []),
        "before_version_id": int(row.before_version_id) if row.before_version_id is not None else None,
        "after_version_id": int(row.after_version_id) if row.after_version_id is not None else None,
        "write_summary": row.write_summary,
        "summary": row.write_summary or "Durable write recorded.",
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "created_at": _dt(row.created_at),
    }


def _serialize_unresolved_for_workspace(row: LocalAgentUnresolvedEntity) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "run_id": int(row.run_id) if row.run_id is not None else None,
        "document_id": int(row.document_id) if row.document_id is not None else None,
        "document_href": f"/docs/{int(row.document_id)}" if row.document_id is not None else None,
        "name": row.name,
        "entity_type": row.entity_type,
        "rationale": row.rationale,
        "status": row.status,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "created_at": _dt(row.created_at),
    }


def _workspace_work_items(
    *,
    agent_documents: list[dict[str, Any]],
    writes: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
    blocked_actions: list[dict[str, Any]],
    blocked_runs: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in agent_documents[:3]:
        items.append(
            {
                "kind": "dispatch",
                "label": row.get("display_kind") or "Analyst dispatch",
                "title": row.get("display_title") or row.get("title") or "Untitled dispatch",
                "summary": f"{len(row.get('source_document_ids') or [])} source documents; {len(row.get('compression_ids') or [])} compressions.",
                "created_at": row.get("updated_at"),
                "tone": "dispatch",
            }
        )
    for row in blocked_actions[:6]:
        label = "Agent error" if str(row.get("status") or "").lower() == "error" else "Agent block"
        items.append(
            {
                "kind": "blocked",
                "label": label,
                "title": row.get("action_kind") or row.get("tool_name") or "Agent action",
                "summary": row.get("summary") or row.get("error") or "Agent action failed.",
                "created_at": row.get("created_at"),
                "tone": "blocked",
            }
        )
    for row in blocked_runs[:6]:
        items.append(
            {
                "kind": "blocked",
                "label": "Run failed",
                "title": f"Run #{row.get('id')} {row.get('run_type') or ''}".strip(),
                "summary": row.get("summary") or row.get("error") or "Run failed.",
                "created_at": row.get("started_at"),
                "tone": "blocked",
            }
        )
    for row in writes[:6]:
        items.append(
            {
                "kind": "write",
                "label": "Durable write",
                "title": row.get("destination_label") or "Research object",
                "summary": row.get("summary") or "Durable write recorded.",
                "created_at": row.get("created_at"),
                "tone": "write",
            }
        )
    for row in review_items[:8]:
        items.append(
            {
                "kind": "review",
                "label": row.get("materiality") or "Needs review",
                "title": row.get("document_title") or f"Document #{row.get('document_id')}",
                "href": row.get("document_href"),
                "summary": row.get("summary_short") or row.get("summary_detail") or row.get("skip_reason") or row.get("error"),
                "created_at": row.get("created_at"),
                "tone": "review",
            }
        )
    for row in unresolved[:5]:
        items.append(
            {
                "kind": "unresolved",
                "label": "Unresolved entity",
                "title": row.get("name") or "Unresolved entity",
                "href": row.get("document_href"),
                "summary": row.get("rationale") or "Needs entity resolution before promotion.",
                "created_at": row.get("created_at"),
                "tone": "blocked",
            }
        )
    return items[:20]


def _workspace_runtime_stage_decision(session: Session) -> dict[str, Any] | None:
    """Use the periodically refreshed supervisor decision for fast UI reads."""
    try:
        if str(session.get_bind().dialect.name or "").lower() != "postgresql":
            return None
        runtime_dir = Path(getattr(settings, "runtime_state_dir", "data/runtime"))
        payload = json.loads((runtime_dir / "raider-adaptive-stage-supervisor.json").read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or not isinstance(payload.get("last_decision"), dict):
            return None
        updated_raw = str(payload.get("updated_at") or "").strip().replace("Z", "+00:00")
        updated = datetime.fromisoformat(updated_raw)
        if updated.tzinfo is not None:
            updated = updated.astimezone(timezone.utc).replace(tzinfo=None)
        if updated < utcnow() - timedelta(minutes=15):
            return None
        decision = dict(payload["last_decision"])
        decision["runtime_stage"] = payload.get("runtime_stage")
        decision["runtime_status"] = payload.get("runtime_status")
        decision["worker_ready"] = payload.get("worker_ready")
        decision["text_endpoint_ready"] = payload.get("text_endpoint_ready")
        decision["workspace_state_source"] = "adaptive_supervisor"
        decision["workspace_state_updated_at"] = payload.get("updated_at")
        return decision
    except Exception:
        return None


def build_agent_workspace_state(session: Session, *, hours: int = 72, limit: int = 80) -> dict[str, Any]:
    bounded_hours = max(1, min(int(hours or 72), 720))
    bounded_limit = _limit(limit, default=80, high=300)
    cutoff = utcnow() - timedelta(hours=bounded_hours)

    agent_documents = (
        session.query(LocalAgentDailySummary)
        .options(
            load_only(
                LocalAgentDailySummary.id,
                LocalAgentDailySummary.run_id,
                LocalAgentDailySummary.summary_date,
                LocalAgentDailySummary.summary_kind,
                LocalAgentDailySummary.title,
                LocalAgentDailySummary.content_md,
                LocalAgentDailySummary.source_document_ids_json,
                LocalAgentDailySummary.compression_ids_json,
                LocalAgentDailySummary.created_at,
                LocalAgentDailySummary.updated_at,
            )
        )
        .filter(LocalAgentDailySummary.updated_at >= cutoff)
        .order_by(LocalAgentDailySummary.updated_at.desc(), LocalAgentDailySummary.id.desc())
        .limit(bounded_limit)
        .all()
    )
    # Pull a bounded recent candidate window first, then apply the intake
    # visibility policy once across those document ids. Applying the policy's
    # correlated filing/universe predicates independently to every workspace
    # query made `/agents` scan the broad document population before LIMIT.
    candidate_limit = min(900, max(bounded_limit * 3, 120))
    compressions = (
        session.query(LocalAgentDocumentCompression)
        .options(
            load_only(
                LocalAgentDocumentCompression.id,
                LocalAgentDocumentCompression.run_id,
                LocalAgentDocumentCompression.document_id,
                LocalAgentDocumentCompression.summary_short,
                LocalAgentDocumentCompression.summary_detail,
                LocalAgentDocumentCompression.atomic_claims_json,
                LocalAgentDocumentCompression.confidence,
                LocalAgentDocumentCompression.information_gain,
                LocalAgentDocumentCompression.document_disposition,
                LocalAgentDocumentCompression.skip_reason,
                LocalAgentDocumentCompression.status,
                LocalAgentDocumentCompression.error,
                LocalAgentDocumentCompression.created_at,
                LocalAgentDocumentCompression.updated_at,
            ),
            joinedload(LocalAgentDocumentCompression.document)
            .load_only(Document.id, Document.title, Document.url, Document.published_at, Document.source_id)
            .joinedload(Document.source)
            .load_only(Source.id, Source.name),
        )
        .filter(LocalAgentDocumentCompression.created_at >= cutoff)
        .order_by(LocalAgentDocumentCompression.created_at.desc(), LocalAgentDocumentCompression.id.desc())
        .limit(candidate_limit)
        .all()
    )
    deep_traces = (
        session.query(LocalAgentDeepAnalysisTrace)
        .options(
            load_only(
                LocalAgentDeepAnalysisTrace.id,
                LocalAgentDeepAnalysisTrace.run_id,
                LocalAgentDeepAnalysisTrace.compression_id,
                LocalAgentDeepAnalysisTrace.document_id,
                LocalAgentDeepAnalysisTrace.analysis_mode,
                LocalAgentDeepAnalysisTrace.status,
                LocalAgentDeepAnalysisTrace.signal_level,
                LocalAgentDeepAnalysisTrace.signal_score,
                LocalAgentDeepAnalysisTrace.summary_md,
                LocalAgentDeepAnalysisTrace.key_findings_json,
                LocalAgentDeepAnalysisTrace.knowledge_connections_json,
                LocalAgentDeepAnalysisTrace.contradictions_json,
                LocalAgentDeepAnalysisTrace.open_questions_json,
                LocalAgentDeepAnalysisTrace.followups_json,
                LocalAgentDeepAnalysisTrace.error,
                LocalAgentDeepAnalysisTrace.created_at,
                LocalAgentDeepAnalysisTrace.updated_at,
            ),
            joinedload(LocalAgentDeepAnalysisTrace.document)
            .load_only(Document.id, Document.title, Document.url, Document.published_at, Document.source_id)
            .joinedload(Document.source)
            .load_only(Source.id, Source.name),
        )
        .filter(LocalAgentDeepAnalysisTrace.created_at >= cutoff)
        .order_by(LocalAgentDeepAnalysisTrace.created_at.desc(), LocalAgentDeepAnalysisTrace.id.desc())
        .limit(candidate_limit)
        .all()
    )
    reads = (
        session.query(LocalAgentDocumentRead)
        .options(
            load_only(
                LocalAgentDocumentRead.id,
                LocalAgentDocumentRead.run_id,
                LocalAgentDocumentRead.document_id,
                LocalAgentDocumentRead.read_kind,
                LocalAgentDocumentRead.created_at,
            ),
            joinedload(LocalAgentDocumentRead.document)
            .load_only(Document.id, Document.title, Document.url, Document.triage_state, Document.source_id)
            .joinedload(Document.source)
            .load_only(Source.id, Source.name),
        )
        .filter(LocalAgentDocumentRead.created_at >= cutoff)
        .order_by(LocalAgentDocumentRead.created_at.desc(), LocalAgentDocumentRead.id.desc())
        .limit(candidate_limit)
        .all()
    )
    writes = (
        session.query(LocalAgentWriteDiff)
        .options(
            load_only(
                LocalAgentWriteDiff.id,
                LocalAgentWriteDiff.run_id,
                LocalAgentWriteDiff.action_id,
                LocalAgentWriteDiff.destination_type,
                LocalAgentWriteDiff.destination_id,
                LocalAgentWriteDiff.source_document_ids_json,
                LocalAgentWriteDiff.before_version_id,
                LocalAgentWriteDiff.after_version_id,
                LocalAgentWriteDiff.write_summary,
                LocalAgentWriteDiff.confidence,
                LocalAgentWriteDiff.created_at,
            )
        )
        .filter(LocalAgentWriteDiff.created_at >= cutoff)
        .order_by(LocalAgentWriteDiff.created_at.desc(), LocalAgentWriteDiff.id.desc())
        .limit(100)
        .all()
    )
    unresolved = (
        session.query(LocalAgentUnresolvedEntity)
        .options(
            load_only(
                LocalAgentUnresolvedEntity.id,
                LocalAgentUnresolvedEntity.run_id,
                LocalAgentUnresolvedEntity.document_id,
                LocalAgentUnresolvedEntity.name,
                LocalAgentUnresolvedEntity.entity_type,
                LocalAgentUnresolvedEntity.rationale,
                LocalAgentUnresolvedEntity.status,
                LocalAgentUnresolvedEntity.confidence,
                LocalAgentUnresolvedEntity.created_at,
            )
        )
        .filter(LocalAgentUnresolvedEntity.created_at >= cutoff)
        .order_by(LocalAgentUnresolvedEntity.created_at.desc(), LocalAgentUnresolvedEntity.id.desc())
        .limit(min(300, candidate_limit))
        .all()
    )
    actions = (
        session.query(LocalAgentAction)
        .options(
            load_only(
                LocalAgentAction.id,
                LocalAgentAction.run_id,
                LocalAgentAction.action_kind,
                LocalAgentAction.status,
                LocalAgentAction.tool_name,
                LocalAgentAction.endpoint,
                LocalAgentAction.document_id,
                LocalAgentAction.destination_type,
                LocalAgentAction.destination_id,
                LocalAgentAction.summary,
                LocalAgentAction.confidence,
                LocalAgentAction.error,
                LocalAgentAction.created_at,
            )
        )
        .filter(LocalAgentAction.created_at >= cutoff)
        .order_by(LocalAgentAction.created_at.desc(), LocalAgentAction.id.desc())
        .limit(min(300, candidate_limit))
        .all()
    )
    runs = (
        session.query(LocalAgentRun)
        .options(
            load_only(
                LocalAgentRun.id,
                LocalAgentRun.run_type,
                LocalAgentRun.status,
                LocalAgentRun.model_profile,
                LocalAgentRun.model_name,
                LocalAgentRun.prompt_version,
                LocalAgentRun.worker_id,
                LocalAgentRun.summary,
                LocalAgentRun.error,
                LocalAgentRun.started_at,
                LocalAgentRun.completed_at,
                LocalAgentRun.created_at,
                LocalAgentRun.updated_at,
            )
        )
        .filter(LocalAgentRun.started_at >= cutoff)
        .order_by(LocalAgentRun.started_at.desc(), LocalAgentRun.id.desc())
        .limit(100)
        .all()
    )

    candidate_document_ids = {
        int(document_id)
        for document_id in [
            *(row.document_id for row in compressions),
            *(row.document_id for row in deep_traces),
            *(row.document_id for row in reads),
            *(row.document_id for row in unresolved),
            *(row.document_id for row in actions),
        ]
        if document_id is not None
    }
    visible_document_ids = set()
    if candidate_document_ids:
        visible_document_ids = {
            int(document_id)
            for (document_id,) in (
                session.query(Document.id)
                .filter(Document.id.in_(sorted(candidate_document_ids)))
                .filter(agent_intake_visible_filter(session))
                .all()
            )
        }
    compressions = [row for row in compressions if int(row.document_id) in visible_document_ids][:bounded_limit]
    deep_traces = [row for row in deep_traces if int(row.document_id) in visible_document_ids][:bounded_limit]
    reads = [row for row in reads if int(row.document_id) in visible_document_ids][:bounded_limit]
    unresolved = [
        row for row in unresolved if row.document_id is None or int(row.document_id) in visible_document_ids
    ][:100]
    actions = [
        row for row in actions if row.document_id is None or int(row.document_id) in visible_document_ids
    ][:100]

    error_count = sum(1 for row in actions if row.status == "error") + sum(1 for row in runs if row.status == "error")
    running_count = sum(1 for row in runs if row.status in RUN_OPEN_STATUSES)
    processed_document_ids = {int(row.document_id) for row in compressions}
    serialized_documents = [serialize_agent_document(row, include_payload=False) for row in agent_documents]
    serialized_compressions = [_serialize_agent_compression_for_workspace(row) for row in compressions]
    for item in serialized_compressions:
        item["materiality"] = _workspace_comp_materiality(item)
        item["needs_review"] = _workspace_comp_needs_review(item)
    material_compressions = sorted(
        [item for item in serialized_compressions if _workspace_comp_is_material(item)],
        key=_workspace_comp_sort_key,
        reverse=True,
    )
    deep_trace_items = [_serialize_deep_trace_for_workspace(row) for row in deep_traces]
    routine_compressions = [item for item in serialized_compressions if not _workspace_comp_is_material(item)]
    review_items = [item for item in material_compressions if item.get("needs_review")]
    write_items = [_serialize_workspace_write(row) for row in writes]
    unresolved_items = [_serialize_unresolved_for_workspace(row) for row in unresolved]
    recent_actions_serialized = [serialize_action(row, include_payloads=False) for row in actions[:30]]
    recent_runs_serialized = [serialize_run(row, include_stats=False) for row in runs[:30]]
    blocked_actions = [serialize_action(row, include_payloads=False) for row in actions if row.status in {"blocked", "error"}]
    blocked_runs = [serialize_run(row, include_stats=False) for row in runs if row.status == "error"]
    work_items = _workspace_work_items(
        agent_documents=serialized_documents,
        writes=write_items,
        review_items=review_items,
        blocked_actions=blocked_actions,
        blocked_runs=blocked_runs,
        unresolved=unresolved_items,
    )

    stage_decision = _workspace_runtime_stage_decision(session)
    try:
        if stage_decision is None and str(session.get_bind().dialect.name or "").lower() == "postgresql":
            stage_decision = local_agent_runtime_stage_decision(
                session,
                media_limit=24,
                signal_limit=2,
                durable_limit=1,
                lookback_hours=min(max(bounded_hours, 24), 96),
                queue_strategy="balanced",
                recent_run_limit=4,
            )
    except Exception as exc:
        stage_decision = {
            "recommendation": "unknown",
            "recommended_stage": "unknown",
            "reason": "stage_decision_unavailable",
            "error_code": "stage_decision_unavailable",
            "media_state_code": "unknown",
            "model_work_state": "unknown",
            "error": str(exc)[:240],
        }

    stage_decision = stage_decision or {
        "recommendation": "unknown",
        "recommended_stage": "unknown",
        "reason": "runtime_stage_decision_unavailable",
        "error_code": "runtime_stage_decision_unavailable",
        "media_state_code": "unknown",
        "model_work_state": "unknown",
    }

    return {
        "hours": bounded_hours,
        "limit": bounded_limit,
        "generated_at": _dt(utcnow()),
        "metrics": {
            "agent_documents": len(agent_documents),
            "processed_documents": len(processed_document_ids),
            "agent_reads": len(reads),
            "durable_writes": len(writes),
            "material_items": len(material_compressions),
            "deep_traces": len(deep_trace_items),
            "routine_items": len(routine_compressions),
            "review_needed": len(review_items) + len(unresolved_items),
            "blocked_items": len(blocked_actions) + len(blocked_runs),
            "running_runs": running_count,
            "errors": error_count,
            "model_work_state": stage_decision.get("model_work_state"),
            "media_state_code": stage_decision.get("media_state_code"),
            "media_actionable_jobs": stage_decision.get("media_actionable_jobs"),
            "media_retry_delayed_jobs": stage_decision.get("media_retry_delayed_jobs"),
            "media_retry_delayed_nonblocking": stage_decision.get("media_retry_delayed_nonblocking"),
        },
        "stage_decision": stage_decision,
        "agent_documents": serialized_documents,
        "work_items": work_items,
        "compressions": serialized_compressions,
        "deep_analysis_traces": deep_trace_items,
        "material_compressions": material_compressions,
        "routine_compressions": routine_compressions,
        "review_items": review_items,
        "reads": [_serialize_agent_read(row) for row in reads],
        "writes": write_items,
        "unresolved_entities": unresolved_items,
        "recent_actions": recent_actions_serialized,
        "recent_runs": recent_runs_serialized,
    }


def build_agent_overview_state(
    session: Session,
    *,
    hours: int = 72,
    review_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the bounded command-surface state without loading provenance bodies."""
    bounded_hours = max(1, min(int(hours or 72), 720))
    cutoff = utcnow() - timedelta(hours=bounded_hours)
    review_state = review_state or {}
    review_metrics = dict(review_state.get("metrics") or {})

    durable_writes = int(
        session.query(func.count(LocalAgentWriteDiff.id))
        .filter(LocalAgentWriteDiff.created_at >= cutoff)
        .scalar()
        or 0
    )
    blocked_actions = int(
        session.query(func.count(LocalAgentAction.id))
        .filter(LocalAgentAction.created_at >= cutoff, LocalAgentAction.status.in_(("error", "blocked")))
        .scalar()
        or 0
    )
    blocked_runs = int(
        session.query(func.count(LocalAgentRun.id))
        .filter(LocalAgentRun.started_at >= cutoff, LocalAgentRun.status == "error")
        .scalar()
        or 0
    )
    running_runs = int(
        session.query(func.count(LocalAgentRun.id))
        .filter(LocalAgentRun.started_at >= cutoff, LocalAgentRun.status == "running")
        .scalar()
        or 0
    )
    queued_outputs = int(
        session.query(func.count(LocalAgentDailySummary.id))
        .filter(LocalAgentDailySummary.updated_at >= cutoff)
        .scalar()
        or 0
    )
    stage_decision = _workspace_runtime_stage_decision(session) or {
        "recommendation": "unknown",
        "recommended_stage": "unknown",
        "reason": "runtime_stage_unavailable",
        "media_state_code": "unknown",
        "model_work_state": "unknown",
    }
    total_review = int(review_state.get("total_items") or review_metrics.get("compressions") or 0)
    return {
        "hours": bounded_hours,
        "generated_at": _dt(utcnow()),
        "metrics": {
            "agent_documents": queued_outputs,
            "processed_documents": total_review,
            "agent_reads": 0,
            "durable_writes": durable_writes,
            "material_items": total_review,
            "deep_traces": 0,
            "routine_items": int(review_metrics.get("skipped") or 0),
            "review_needed": total_review,
            "blocked_items": blocked_actions + blocked_runs,
            "running_runs": running_runs,
            "errors": blocked_actions + blocked_runs,
            "model_work_state": stage_decision.get("model_work_state"),
            "media_state_code": stage_decision.get("media_state_code"),
            "media_actionable_jobs": stage_decision.get("media_actionable_jobs"),
            "media_retry_delayed_jobs": stage_decision.get("media_retry_delayed_jobs"),
            "media_retry_delayed_nonblocking": stage_decision.get("media_retry_delayed_nonblocking"),
        },
        "stage_decision": stage_decision,
        "work_items": [],
        "agent_documents": [],
        "compressions": [],
        "deep_analysis_traces": [],
        "material_compressions": [],
        "routine_compressions": [],
        "review_items": [],
        "reads": [],
        "writes": [],
        "unresolved_entities": [],
        "recent_actions": [],
        "recent_runs": [],
    }


def daily_summary_context(session: Session, *, hours: int = 24, limit: int = 250) -> dict[str, Any]:
    bounded_hours = max(1, min(int(hours or 24), 168))
    bounded_limit = _limit(limit, default=250, high=1000)
    cutoff = utcnow() - timedelta(hours=bounded_hours)
    # Resolve visibility once for the documents referenced by this lookback,
    # then select each rail's bounded IDs before loading projected payloads.
    from sqlalchemy import literal, select, union_all

    rails = (
        ("compressions", LocalAgentDocumentCompression, bounded_limit, False),
        ("deep_traces", LocalAgentDeepAnalysisTrace, min(bounded_limit, 100), False),
        ("claims", LocalAgentEntityClaim, bounded_limit, True),
        ("routes", LocalAgentEntityRoute, bounded_limit, True),
        ("unresolved", LocalAgentUnresolvedEntity, 100, True),
        ("errors", LocalAgentAction, 100, True),
    )
    candidate_queries = []
    for name, model, _, _ in rails:
        candidate_query = select(model.document_id).where(
            model.created_at >= cutoff, model.document_id.isnot(None)
        )
        if name == "errors":
            candidate_query = candidate_query.where(model.status == "error")
        candidate_queries.append(candidate_query)
    candidate_documents = union_all(*candidate_queries).subquery()
    visible_documents = (
        _agent_visible_document_ids(session)
        .filter(Document.id.in_(select(candidate_documents.c.document_id)))
        .statement.cte("daily_summary_visible_documents")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    rail_queries = []
    for name, model, rail_limit, allow_null in rails:
        visible = model.document_id.in_(select(visible_documents.c.id))
        if allow_null:
            visible = or_(model.document_id.is_(None), visible)
        rail_query = select(model.id).where(model.created_at >= cutoff, visible)
        if name == "errors":
            rail_query = rail_query.where(model.status == "error")
        bounded_rail = (
            rail_query.order_by(model.created_at.desc(), model.id.desc())
            .limit(rail_limit).subquery()
        )
        rail_queries.append(select(literal(name).label("rail"), bounded_rail.c.id))
    rail_ids: dict[str, list[int]] = {name: [] for name, _, _, _ in rails}
    for name, row_id in session.execute(union_all(*rail_queries)):
        rail_ids[name].append(int(row_id))
    compression_rows = (
        session.query(LocalAgentDocumentCompression)
        .options(
            load_only(
                LocalAgentDocumentCompression.id,
                LocalAgentDocumentCompression.run_id,
                LocalAgentDocumentCompression.document_id,
                LocalAgentDocumentCompression.summary_short,
                LocalAgentDocumentCompression.summary_detail,
                LocalAgentDocumentCompression.atomic_claims_json,
                LocalAgentDocumentCompression.entity_refs_json,
                LocalAgentDocumentCompression.supply_chain_refs_json,
                LocalAgentDocumentCompression.technology_refs_json,
                LocalAgentDocumentCompression.confidence,
                LocalAgentDocumentCompression.information_gain,
                LocalAgentDocumentCompression.document_disposition,
                LocalAgentDocumentCompression.skip_reason,
                LocalAgentDocumentCompression.status,
                LocalAgentDocumentCompression.error,
                LocalAgentDocumentCompression.created_at,
            ),
            joinedload(LocalAgentDocumentCompression.document)
            .load_only(Document.id, Document.title, Document.published_at, Document.source_id)
            .joinedload(Document.source)
            .load_only(Source.id, Source.name),
        )
        .filter(LocalAgentDocumentCompression.created_at >= cutoff)
        .filter(LocalAgentDocumentCompression.id.in_(rail_ids["compressions"]))
        .order_by(LocalAgentDocumentCompression.created_at.desc(), LocalAgentDocumentCompression.id.desc())
        .limit(bounded_limit)
        .all()
    )
    deep_trace_rows = (
        session.query(LocalAgentDeepAnalysisTrace)
        .options(
            load_only(
                LocalAgentDeepAnalysisTrace.id,
                LocalAgentDeepAnalysisTrace.run_id,
                LocalAgentDeepAnalysisTrace.compression_id,
                LocalAgentDeepAnalysisTrace.document_id,
                LocalAgentDeepAnalysisTrace.analysis_mode,
                LocalAgentDeepAnalysisTrace.status,
                LocalAgentDeepAnalysisTrace.signal_level,
                LocalAgentDeepAnalysisTrace.signal_score,
                LocalAgentDeepAnalysisTrace.summary_md,
                LocalAgentDeepAnalysisTrace.key_findings_json,
                LocalAgentDeepAnalysisTrace.knowledge_connections_json,
                LocalAgentDeepAnalysisTrace.contradictions_json,
                LocalAgentDeepAnalysisTrace.open_questions_json,
                LocalAgentDeepAnalysisTrace.followups_json,
                LocalAgentDeepAnalysisTrace.error,
                LocalAgentDeepAnalysisTrace.created_at,
            )
        )
        .filter(LocalAgentDeepAnalysisTrace.created_at >= cutoff)
        .filter(LocalAgentDeepAnalysisTrace.id.in_(rail_ids["deep_traces"]))
        .order_by(LocalAgentDeepAnalysisTrace.created_at.desc(), LocalAgentDeepAnalysisTrace.id.desc())
        .limit(min(bounded_limit, 100))
        .all()
    )
    claim_rows = (
        session.query(LocalAgentEntityClaim)
        .options(
            load_only(
                LocalAgentEntityClaim.id,
                LocalAgentEntityClaim.run_id,
                LocalAgentEntityClaim.compression_id,
                LocalAgentEntityClaim.document_id,
                LocalAgentEntityClaim.entity_type,
                LocalAgentEntityClaim.entity_id,
                LocalAgentEntityClaim.entity_name,
                LocalAgentEntityClaim.destination_type,
                LocalAgentEntityClaim.destination_id,
                LocalAgentEntityClaim.claim_kind,
                LocalAgentEntityClaim.claim_text,
                LocalAgentEntityClaim.confidence,
                LocalAgentEntityClaim.routing_reason,
                LocalAgentEntityClaim.created_at,
            )
        )
        .filter(LocalAgentEntityClaim.created_at >= cutoff)
        .filter(LocalAgentEntityClaim.id.in_(rail_ids["claims"]))
        .order_by(LocalAgentEntityClaim.created_at.desc(), LocalAgentEntityClaim.id.desc())
        .limit(bounded_limit)
        .all()
    )
    route_rows = (
        session.query(LocalAgentEntityRoute)
        .options(
            load_only(
                LocalAgentEntityRoute.id,
                LocalAgentEntityRoute.run_id,
                LocalAgentEntityRoute.compression_id,
                LocalAgentEntityRoute.document_id,
                LocalAgentEntityRoute.lane,
                LocalAgentEntityRoute.entity_name,
                LocalAgentEntityRoute.entity_type,
                LocalAgentEntityRoute.entity_role,
                LocalAgentEntityRoute.summary_relevance,
                LocalAgentEntityRoute.summary_confidence,
                LocalAgentEntityRoute.durable_relevance,
                LocalAgentEntityRoute.durable_confidence,
                LocalAgentEntityRoute.durable_action,
                LocalAgentEntityRoute.materiality,
                LocalAgentEntityRoute.novelty,
                LocalAgentEntityRoute.temporal_posture,
                LocalAgentEntityRoute.evidence_tier,
                LocalAgentEntityRoute.resolution_status,
                LocalAgentEntityRoute.new_information,
                LocalAgentEntityRoute.rationale,
                LocalAgentEntityRoute.claim_refs_json,
                LocalAgentEntityRoute.source_claim_indexes_json,
                LocalAgentEntityRoute.destination_type,
                LocalAgentEntityRoute.destination_id,
                LocalAgentEntityRoute.created_at,
            )
        )
        .filter(LocalAgentEntityRoute.created_at >= cutoff)
        .filter(LocalAgentEntityRoute.id.in_(rail_ids["routes"]))
        .order_by(LocalAgentEntityRoute.created_at.desc(), LocalAgentEntityRoute.id.desc())
        .limit(bounded_limit)
        .all()
    )
    write_rows = (
        session.query(LocalAgentWriteDiff)
        .options(
            load_only(
                LocalAgentWriteDiff.id,
                LocalAgentWriteDiff.destination_type,
                LocalAgentWriteDiff.destination_id,
                LocalAgentWriteDiff.source_document_ids_json,
                LocalAgentWriteDiff.diff_text,
                LocalAgentWriteDiff.write_summary,
                LocalAgentWriteDiff.confidence,
                LocalAgentWriteDiff.created_at,
            )
        )
        .filter(LocalAgentWriteDiff.created_at >= cutoff)
        .order_by(LocalAgentWriteDiff.created_at.desc(), LocalAgentWriteDiff.id.desc())
        .limit(100)
        .all()
    )
    unresolved_rows = (
        session.query(LocalAgentUnresolvedEntity)
        .options(
            load_only(
                LocalAgentUnresolvedEntity.id,
                LocalAgentUnresolvedEntity.document_id,
                LocalAgentUnresolvedEntity.name,
                LocalAgentUnresolvedEntity.entity_type,
                LocalAgentUnresolvedEntity.rationale,
                LocalAgentUnresolvedEntity.status,
                LocalAgentUnresolvedEntity.confidence,
                LocalAgentUnresolvedEntity.created_at,
            )
        )
        .filter(LocalAgentUnresolvedEntity.created_at >= cutoff)
        .filter(LocalAgentUnresolvedEntity.id.in_(rail_ids["unresolved"]))
        .order_by(LocalAgentUnresolvedEntity.created_at.desc(), LocalAgentUnresolvedEntity.id.desc())
        .limit(100)
        .all()
    )
    error_rows = (
        session.query(LocalAgentAction)
        .options(
            load_only(
                LocalAgentAction.id,
                LocalAgentAction.run_id,
                LocalAgentAction.action_kind,
                LocalAgentAction.status,
                LocalAgentAction.tool_name,
                LocalAgentAction.endpoint,
                LocalAgentAction.document_id,
                LocalAgentAction.destination_type,
                LocalAgentAction.destination_id,
                LocalAgentAction.summary,
                LocalAgentAction.confidence,
                LocalAgentAction.error,
                LocalAgentAction.created_at,
            )
        )
        .filter(LocalAgentAction.created_at >= cutoff, LocalAgentAction.status == "error")
        .filter(LocalAgentAction.id.in_(rail_ids["errors"]))
        .order_by(LocalAgentAction.created_at.desc(), LocalAgentAction.id.desc())
        .limit(100)
        .all()
    )
    run_rows = (
        session.query(LocalAgentRun)
        .options(
            load_only(
                LocalAgentRun.id,
                LocalAgentRun.run_type,
                LocalAgentRun.status,
                LocalAgentRun.model_profile,
                LocalAgentRun.model_name,
                LocalAgentRun.prompt_version,
                LocalAgentRun.worker_id,
                LocalAgentRun.summary,
                LocalAgentRun.error,
                LocalAgentRun.started_at,
                LocalAgentRun.completed_at,
                LocalAgentRun.created_at,
                LocalAgentRun.updated_at,
            )
        )
        .filter(LocalAgentRun.started_at >= cutoff)
        .order_by(LocalAgentRun.started_at.desc(), LocalAgentRun.id.desc())
        .limit(100)
        .all()
    )

    source_document_ids: list[int] = []
    compression_ids: list[int] = []
    compressions: list[dict[str, Any]] = []
    for row in compression_rows:
        doc = row.document
        doc_id = int(row.document_id)
        if doc_id not in source_document_ids:
            source_document_ids.append(doc_id)
        compression_ids.append(int(row.id))
        compressions.append(
            {
                "id": int(row.id),
                "document_id": doc_id,
                "title": doc.title if doc is not None else None,
                "source_name": doc.source.name if doc is not None and doc.source is not None else None,
                "published_at": _dt(doc.published_at) if doc is not None else None,
                "summary_short": row.summary_short,
                "summary_detail": _shorten(row.summary_detail, 1200),
                "information_gain": row.information_gain,
                "document_disposition": row.document_disposition,
                "skip_reason": row.skip_reason,
                "status": row.status,
                "error": _shorten(row.error, 500),
                "atomic_claims": list(row.atomic_claims_json or []),
                "entity_refs": list(row.entity_refs_json or []),
                "supply_chain_refs": list(row.supply_chain_refs_json or []),
                "technology_refs": list(row.technology_refs_json or []),
                "confidence": float(row.confidence) if row.confidence is not None else None,
                "created_at": _dt(row.created_at),
            }
        )

    summary_routes: list[dict[str, Any]] = []
    durable_routes: list[dict[str, Any]] = []
    for row in route_rows:
        if row.document_id is not None and int(row.document_id) not in source_document_ids:
            source_document_ids.append(int(row.document_id))
        payload = _serialize_route_for_daily_summary(row)
        if row.summary_relevance:
            summary_routes.append(payload)
        if row.durable_relevance:
            durable_routes.append(payload)

    writes: list[dict[str, Any]] = []
    for row in write_rows:
        for doc_id in list(row.source_document_ids_json or []):
            if int(doc_id) not in source_document_ids:
                source_document_ids.append(int(doc_id))
        writes.append(
            {
                "id": int(row.id),
                "destination_type": row.destination_type,
                "destination_id": int(row.destination_id) if row.destination_id is not None else None,
                "source_document_ids": list(row.source_document_ids_json or []),
                "write_summary": row.write_summary,
                "confidence": float(row.confidence) if row.confidence is not None else None,
                "diff_text": _shorten(row.diff_text, 3000),
                "created_at": _dt(row.created_at),
            }
        )

    return {
        "generated_at": _dt(utcnow()),
        "lookback_hours": bounded_hours,
        "source_document_ids": source_document_ids,
        "compression_ids": compression_ids,
        "runs": [serialize_run(row, include_stats=False) for row in run_rows if row.status == "error"],
        "compressions": compressions,
        "deep_analysis_traces": [serialize_deep_analysis_trace(row, include_payload=False) for row in deep_trace_rows],
        "claims": [_serialize_claim_for_daily_summary(row) for row in claim_rows],
        "summary_routes": summary_routes,
        "durable_routes": durable_routes,
        "writes": writes,
        "unresolved_entities": [
            {
                "id": int(row.id),
                "document_id": int(row.document_id) if row.document_id is not None else None,
                "name": row.name,
                "entity_type": row.entity_type,
                "rationale": row.rationale,
                "status": row.status,
                "confidence": float(row.confidence) if row.confidence is not None else None,
                "created_at": _dt(row.created_at),
            }
            for row in unresolved_rows
        ],
        "errors": [serialize_action(row, include_payloads=False) for row in error_rows],
    }


def recent_actions(
    session: Session,
    *,
    hours: int = 72,
    limit: int = 200,
    include_payloads: bool = False,
    action_kind: str | None = None,
) -> list[dict[str, Any]]:
    cutoff = utcnow() - timedelta(hours=max(1, min(int(hours or 72), 720)))
    query = session.query(LocalAgentAction)
    if not include_payloads:
        query = query.options(
            load_only(
                LocalAgentAction.id,
                LocalAgentAction.run_id,
                LocalAgentAction.action_kind,
                LocalAgentAction.status,
                LocalAgentAction.tool_name,
                LocalAgentAction.endpoint,
                LocalAgentAction.document_id,
                LocalAgentAction.destination_type,
                LocalAgentAction.destination_id,
                LocalAgentAction.summary,
                LocalAgentAction.confidence,
                LocalAgentAction.error,
                LocalAgentAction.created_at,
            )
        )
    kind = str(action_kind or "").strip()
    if kind:
        query = query.filter(LocalAgentAction.action_kind == kind[:64])
    rows = (
        query.filter(LocalAgentAction.created_at >= cutoff)
        .order_by(LocalAgentAction.created_at.desc(), LocalAgentAction.id.desc())
        .limit(_limit(limit, default=200, high=100 if include_payloads else 1000))
        .all()
    )
    return [serialize_action(row, include_payloads=include_payloads) for row in rows]


def recent_runs(session: Session, *, hours: int = 72, limit: int = 100) -> list[dict[str, Any]]:
    cutoff = utcnow() - timedelta(hours=max(1, min(int(hours or 72), 720)))
    rows = (
        session.query(LocalAgentRun)
        .filter(LocalAgentRun.started_at >= cutoff)
        .order_by(LocalAgentRun.started_at.desc(), LocalAgentRun.id.desc())
        .limit(_limit(limit, default=100, high=1000))
        .all()
    )
    return [serialize_run(row) for row in rows]
